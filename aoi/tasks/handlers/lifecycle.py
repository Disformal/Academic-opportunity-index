"""The posting lifecycle (ADR 0033 §3–5): one source's postings evaluated against the evidence.

Order, first match wins (the ported closure.py):
1. a suspected soft 404 (the detail page's latest fetch is a 2xx, extracted, that does not show
   the posting) changes nothing and is logged;
2. the detail page gone (404/410) on consecutive fetches spanning ≥ 24 h → removed;
3. a resolved `status` of "closed" → closed(stated_closed);
4. missed ≥ 3 listing passes → stale; ≥ 7 days after the third missed pass → closed(delisted);
5. the stated deadline passed by > 24 h and the status not "open" → closed(deadline_passed);
6. otherwise active, or pending without the fields the database requires of an active posting.

A missed pass: every listing or feed target of the source that is not retired has a successful (2xx
or 304), extracted fetch after the posting's last sighting on a listing. Detail pages do not count,
or a delisted posting whose page still loads would never close. `disappeared_at` is when the first
missed pass completed, set while the posting has missed enough passes to be stale and cleared
otherwise.

Changes are applied only if no more than the breaker allows leave `active` at once
(ontology/closure_policy.yaml); otherwise nothing is applied and MassClosureHeld is raised. A
changed posting gets a resolve task, which versions the change. A posting that becomes stale has
its detail page fetched next.
"""

import uuid
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aoi.adapters.protocol import CrawlTargetSpec
from aoi.db.schema.core import ExtractionRun, Posting, PostingHistory, PostingSighting, Source
from aoi.db.schema.enums import ClosureReason, PostingState, TargetRole
from aoi.db.schema.evidence import Fetch
from aoi.db.schema.ops import CrawlTarget, Task
from aoi.pipeline import circuit_breaker
from aoi.tasks.handlers.resolve import queue_resolve, stated_time
from aoi.tasks.targets import add_target

log = structlog.get_logger()

ABSENT_PASSES = 3
DELISTED_AFTER = timedelta(days=7)
DEADLINE_GRACE = timedelta(hours=24)
GONE_SPAN = timedelta(hours=24)
REQUIRED_FOR_ACTIVE = ("title", "application_url")  # the provenance trigger enforces these
_LISTING_ROLES = (TargetRole.LISTING, TargetRole.FEED)


class MassClosureHeld(Exception):
    """Too many postings would leave `active` in one evaluation; nothing was applied."""


@dataclass(frozen=True)
class _DetailFetch:
    fetch_id: uuid.UUID
    at: datetime
    status: int | None
    extracted: bool


@dataclass(frozen=True)
class _Decision:
    state: PostingState
    closure_reason: ClosureReason | None
    disappeared_at: datetime | None


def evaluate(
    session: Session, source_id: uuid.UUID, *, now: datetime, allow_mass_closure: bool = False
) -> Counter[str]:
    """Apply the source's lifecycle changes in the caller's transaction; count the new states."""
    source = session.get_one(Source, source_id)
    passes = _listing_fetch_times(session, source_id)
    last_listed = _last_listing_sightings(session, source_id)
    details = _detail_fetches(session, source_id)
    shown_on_detail = _detail_sightings(session, source_id)
    rows = session.execute(
        select(Posting, PostingHistory.snapshot)
        .outerjoin(
            PostingHistory,
            (PostingHistory.posting_id == Posting.id) & PostingHistory.observed_to.is_(None),
        )
        .where(Posting.source_id == source_id)
    ).all()

    changes: dict[Posting, _Decision] = {}
    for posting, snapshot in rows:
        fields: dict[str, Any] = snapshot["fields"] if snapshot else {}
        history = details.get(posting.detail_url or "", [])
        decision = _decide(
            posting, fields, passes, last_listed.get(posting.id), history, shown_on_detail, now
        )
        if decision != _Decision(posting.state, posting.closure_reason, posting.disappeared_at):
            changes[posting] = decision

    active = sum(1 for posting, _ in rows if posting.state == PostingState.ACTIVE)
    leaving = sum(
        1 for p, d in changes.items() if p.state == PostingState.ACTIVE and d.state != p.state
    )
    breaker = circuit_breaker.check(
        active_before=active, would_close=leaving, source_name=source.name
    )
    if not breaker.allowed and not allow_mass_closure:
        raise MassClosureHeld(breaker.message)

    for posting, decision in changes.items():
        becomes_stale = (
            decision.state == PostingState.STALE and posting.state != PostingState.STALE
        )
        if becomes_stale and posting.detail_url is not None:
            spec = CrawlTargetSpec(url=posting.detail_url, role=TargetRole.DETAIL)
            add_target(session, source_id, spec).next_due_at = now
        posting.state = decision.state
        posting.closure_reason = decision.closure_reason
        posting.disappeared_at = decision.disappeared_at
        session.flush()
        queue_resolve(session, source_id, posting.id)

    counts = Counter(d.state.value for d in changes.values())
    log.info("lifecycle.done", source=source.name, changed=len(changes), **counts)
    return counts


def _decide(
    posting: Posting,
    fields: dict[str, Any],
    passes: list[list[datetime]],
    last_listed: datetime | None,
    detail: list[_DetailFetch],
    shown_on_detail: set[tuple[uuid.UUID, uuid.UUID]],
    now: datetime,
) -> _Decision:
    latest = detail[-1] if detail else None
    if (
        latest is not None
        and latest.status is not None
        and 200 <= latest.status < 300
        and latest.extracted
        and (posting.id, latest.fetch_id) not in shown_on_detail
    ):
        log.warning(
            "lifecycle.soft_404", posting_id=str(posting.id), fetch_id=str(latest.fetch_id)
        )
        return _Decision(posting.state, posting.closure_reason, posting.disappeared_at)

    missed, first_missed, third_missed = _missed(passes, last_listed)
    disappeared = first_missed if missed >= ABSENT_PASSES else None

    gone: list[_DetailFetch] = []
    for fetch in reversed(detail):
        if fetch.status not in (404, 410):
            break
        gone.append(fetch)
    if len(gone) >= 2 and gone[0].at - gone[-1].at >= GONE_SPAN:
        return _Decision(PostingState.REMOVED, None, disappeared)

    status = fields.get("status")
    if status == "closed":
        return _Decision(PostingState.CLOSED, ClosureReason.STATED_CLOSED, disappeared)
    if missed >= ABSENT_PASSES:
        assert third_missed is not None
        if now - third_missed >= DELISTED_AFTER:
            return _Decision(PostingState.CLOSED, ClosureReason.DELISTED, disappeared)
        return _Decision(PostingState.STALE, None, disappeared)
    deadline = stated_time(fields.get("deadline"))
    if deadline is not None and deadline < now - DEADLINE_GRACE and status != "open":
        return _Decision(PostingState.CLOSED, ClosureReason.DEADLINE_PASSED, disappeared)
    if all(name in fields for name in REQUIRED_FOR_ACTIVE):
        return _Decision(PostingState.ACTIVE, None, disappeared)
    return _Decision(PostingState.PENDING, None, disappeared)


def _missed(
    passes: list[list[datetime]], last_listed: datetime | None
) -> tuple[int, datetime | None, datetime | None]:
    """Passes missed since `last_listed`, and when the first and third of them completed."""
    if last_listed is None or not passes:
        return 0, None, None
    after = [times[bisect_right(times, last_listed) :] for times in passes]
    missed = min(len(times) for times in after)
    first = max(times[0] for times in after) if missed >= 1 else None
    third = max(times[2] for times in after) if missed >= 3 else None
    return missed, first, third


def _extracted() -> Any:
    return (
        select(ExtractionRun.id)
        .where(ExtractionRun.fetch_id == Fetch.id, ExtractionRun.finished_at.is_not(None))
        .exists()
    )


def _listing_fetch_times(session: Session, source_id: uuid.UUID) -> list[list[datetime]]:
    """Per listing or feed target, the sorted times of its successful, extracted fetches."""
    targets = session.scalars(
        select(CrawlTarget.id).where(
            CrawlTarget.source_id == source_id,
            CrawlTarget.role.in_(_LISTING_ROLES),
            CrawlTarget.retired_at.is_(None),
        )
    ).all()
    times: dict[uuid.UUID, list[datetime]] = {t: [] for t in targets}
    for target_id, at in session.execute(
        select(Fetch.crawl_target_id, Fetch.requested_at)
        .where(
            Fetch.crawl_target_id.in_(targets),
            or_(Fetch.http_status.between(200, 299), Fetch.http_status == 304),
            _extracted(),
        )
        .order_by(Fetch.requested_at)
    ):
        times[target_id].append(at)
    return list(times.values())


def _last_listing_sightings(session: Session, source_id: uuid.UUID) -> dict[uuid.UUID, datetime]:
    return dict(
        session.execute(
            select(PostingSighting.posting_id, func.max(PostingSighting.observed_at))
            .join(Fetch, Fetch.id == PostingSighting.fetch_id)
            .join(CrawlTarget, CrawlTarget.id == Fetch.crawl_target_id)
            .where(CrawlTarget.source_id == source_id, CrawlTarget.role.in_(_LISTING_ROLES))
            .group_by(PostingSighting.posting_id)
        ).all()
    )


def _detail_fetches(session: Session, source_id: uuid.UUID) -> dict[str, list[_DetailFetch]]:
    """Per detail page URL, its fetches in time order."""
    history: dict[str, list[_DetailFetch]] = defaultdict(list)
    for url, fetch_id, at, status, extracted in session.execute(
        select(CrawlTarget.url, Fetch.id, Fetch.requested_at, Fetch.http_status, _extracted())
        .join(Fetch, Fetch.crawl_target_id == CrawlTarget.id)
        .where(CrawlTarget.source_id == source_id, CrawlTarget.role == TargetRole.DETAIL)
        .order_by(Fetch.requested_at)
    ):
        history[url].append(_DetailFetch(fetch_id, at, status, extracted))
    return history


def _detail_sightings(session: Session, source_id: uuid.UUID) -> set[tuple[uuid.UUID, uuid.UUID]]:
    rows = session.execute(
        select(PostingSighting.posting_id, PostingSighting.fetch_id)
        .join(Fetch, Fetch.id == PostingSighting.fetch_id)
        .join(CrawlTarget, CrawlTarget.id == Fetch.crawl_target_id)
        .where(CrawlTarget.source_id == source_id, CrawlTarget.role == TargetRole.DETAIL)
    ).all()
    return {(posting_id, fetch_id) for posting_id, fetch_id in rows}


def handle_lifecycle(session: Session, task: Task) -> None:
    evaluate(session, task.source_id, now=datetime.now(UTC))
