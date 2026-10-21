"""The resolve step (ADR 0033 §6): a posting's resolved fields, versioned in core.posting_history.

Per field the newest assertion wins; at the same observed time the stronger method wins (official
API > structured markup > template > LLM), then the higher confidence, then the assertion id, so
the result does not depend on the order rows were written. The snapshot holds the resolved fields
plus the lifecycle state and closure reason, which a field change carries over from the version
before (the lifecycle owns them).

History is append-only in observed time: a version is written for each observed time after the
current version's at which the resolved fields change, so a backlog of observations, or a replay
from empty, becomes one version per change. Then, if the posting's lifecycle state changed (the
lifecycle sets it and queues a resolve), or older evidence extracted late changed the resolved
values, one more version is written at the time of resolving: history is never rewritten, and
nothing is dropped. A date-only `deadline` or `posted_at` is stored as midnight UTC in the stated
(valid-time) columns; the snapshot keeps the source's form.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from aoi.db.schema.core import Assertion, Posting, PostingHistory
from aoi.db.schema.enums import AssertionMethod, TaskState
from aoi.db.schema.ops import Task

log = structlog.get_logger()

METHOD_RANK = {
    AssertionMethod.OFFICIAL_API: 3,
    AssertionMethod.STRUCTURED_MARKUP: 2,
    AssertionMethod.TEMPLATE: 1,
    AssertionMethod.LLM: 0,
}


def _rank(a: Assertion) -> tuple[datetime, int, Decimal, uuid.UUID]:
    return (a.observed_at, METHOD_RANK[a.method], a.confidence, a.id)


def _fields_as_of(assertions: Sequence[Assertion], at: datetime) -> dict[str, Any]:
    winners: dict[str, Assertion] = {}
    for a in assertions:
        if a.observed_at <= at and (a.field not in winners or _rank(a) > _rank(winners[a.field])):
            winners[a.field] = a
    return {field: a.value for field, a in sorted(winners.items())}


def stated_time(value: str | None) -> datetime | None:
    """A stated date or time from a resolved field; a bare date is midnight UTC."""
    if value is None:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def resolve(session: Session, posting_id: uuid.UUID) -> int:
    """Append the posting's new history versions in the caller's transaction; return how many."""
    posting = session.get_one(Posting, posting_id)
    assertions = session.scalars(select(Assertion).where(Assertion.posting_id == posting_id)).all()
    current = session.scalars(
        select(PostingHistory)
        .where(PostingHistory.posting_id == posting_id, PostingHistory.observed_to.is_(None))
        .with_for_update()
    ).first()
    lifecycle = {"state": posting.state.value, "closure_reason": posting.closure_reason}
    previous: dict[str, Any] = (
        current.snapshot if current is not None else {"fields": {}, **lifecycle}
    )
    times = sorted(
        {
            a.observed_at
            for a in assertions
            if current is None or a.observed_at > current.observed_from
        }
    )
    written = 0
    for at in times:
        fields = _fields_as_of(assertions, at)
        if fields != previous["fields"]:
            previous = {**previous, "fields": fields}
            current = _append(session, posting_id, current, previous, at)
            written += 1
    latest = {"fields": _fields_as_of(assertions, datetime.max.replace(tzinfo=UTC)), **lifecycle}
    if latest != previous:
        current = _append(session, posting_id, current, latest, datetime.now(UTC))
        written += 1
    log.info("resolve.done", posting_id=str(posting_id), versions=written)
    return written


def _append(
    session: Session,
    posting_id: uuid.UUID,
    current: PostingHistory | None,
    snapshot: dict[str, Any],
    at: datetime,
) -> PostingHistory:
    """Close the current version at `at` and make `snapshot` the current one from `at`."""
    if current is not None:
        current.observed_to = at
        session.flush()  # the old version stops being current before the new one is
    version = PostingHistory(
        posting_id=posting_id,
        observed_from=at,
        stated_posted_at=stated_time(snapshot["fields"].get("posted_at")),
        stated_deadline_at=stated_time(snapshot["fields"].get("deadline")),
        snapshot=snapshot,
    )
    session.add(version)
    session.flush()
    return version


def queue_resolve(session: Session, source_id: uuid.UUID, posting_id: uuid.UUID) -> None:
    """Queue a resolve task for the posting unless one is already waiting.

    A running one does not count: it may have read the posting before the caller's commit.
    """
    waiting = session.scalar(
        select(Task.id).where(
            Task.kind == "resolve",
            Task.state.in_([TaskState.QUEUED, TaskState.FAILED]),
            Task.payload["posting_id"].astext == str(posting_id),
        )
    )
    if waiting is None:
        session.add(
            Task(kind="resolve", source_id=source_id, payload={"posting_id": str(posting_id)})
        )


def explain(session: Session, posting_id: uuid.UUID) -> dict[str, list[dict[str, Any]]]:
    """Every field's values with their evidence, ranked as resolve ranks them (winner first)."""
    assertions = session.scalars(select(Assertion).where(Assertion.posting_id == posting_id)).all()
    report: dict[str, list[dict[str, Any]]] = {}
    for a in sorted(assertions, key=_rank, reverse=True):
        report.setdefault(a.field, []).append(
            {
                "assertion_id": a.id,
                "value": a.value,
                "method": a.method,
                "confidence": a.confidence,
                "observed_at": a.observed_at,
                "fetch_id": a.fetch_id,
                "pointer": a.pointer,
                "span_start": a.span_start,
                "span_end": a.span_end,
                "quote": a.quote,
            }
        )
    return report


def handle_resolve(session: Session, task: Task) -> None:
    resolve(session, uuid.UUID(task.payload["posting_id"]))
