"""The posting lifecycle (ADR 0033 §3–5), evaluated per source.

Order (first match wins): a suspected soft 404 changes nothing; a detail page gone (404/410) twice
≥ 24 h apart → removed; a stated `closed` status → closed(stated_closed); missed ≥ 3 listing passes
→ stale, and stale for ≥ 7 days → closed(delisted); deadline passed by > 24 h and not stated open →
closed(deadline_passed); otherwise active, or pending without the fields the database requires.
A pass is complete when every listing target of the source has a successful, extracted fetch
after the posting's last listing sighting; detail-page sightings do not count. If one evaluation
would move too many postings out of active, nothing is applied (MassClosureHeld).

These tests really commit; the add_target fixture truncates what they write.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from aoi.db.schema.core import (
    Assertion,
    ExtractionRun,
    Posting,
    PostingHistory,
    PostingSighting,
    Source,
)
from aoi.db.schema.enums import (
    AssertionMethod,
    ClosureReason,
    FetchOutcome,
    LegalBasis,
    PostingIdentityKind,
    PostingState,
    TargetRole,
    TaskState,
)
from aoi.db.schema.evidence import Fetch
from aoi.db.schema.ops import CrawlTarget, Task
from aoi.tasks.handlers import handlers_for
from aoi.tasks.handlers.lifecycle import MassClosureHeld, evaluate
from aoi.tasks.handlers.resolve import resolve
from aoi.tasks.worker import run_once

pytestmark = pytest.mark.integration

AddTarget = Callable[..., uuid.UUID]
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)
D = timedelta(days=1)
HOST = "https://queue-test-lifecycle.example.org"
ACTIVE_FIELDS: dict[str, Any] = {
    "title": "Postdoc in hep-th",
    "application_url": f"{HOST}/apply/1",
}


def _fetch(
    engine: Engine,
    target_id: uuid.UUID,
    at: datetime,
    *,
    status: int = 200,
    extracted: bool = True,
) -> uuid.UUID:
    """A committed fetch of the target; with a finished extraction run unless told otherwise."""
    with Session(engine) as session, session.begin():
        target = session.get_one(CrawlTarget, target_id)
        fetch = Fetch(
            crawl_target_id=target_id,
            url=target.url,
            requested_at=at,
            outcome=FetchOutcome.NOT_MODIFIED if status == 304 else FetchOutcome.HIT,
            http_status=status,
        )
        session.add(fetch)
        session.flush()
        if extracted:
            session.add(
                ExtractionRun(
                    fetch_id=fetch.id,
                    extractor="fake-api",
                    extractor_version="1.0.0",
                    started_at=at,
                    finished_at=at,
                )
            )
        return fetch.id


def _posting(
    engine: Engine,
    source_id: uuid.UUID,
    shown_by: list[uuid.UUID],
    fields: dict[str, Any] | None = None,
    *,
    key: str | None = None,
    detail_url: str | None = None,
) -> uuid.UUID:
    """A posting sighted by the given fetches, asserted from the first, and resolved."""
    fields = ACTIVE_FIELDS if fields is None else fields
    with Session(engine) as session, session.begin():
        fetches = [session.get_one(Fetch, f) for f in shown_by]
        times = [f.requested_at for f in fetches]
        posting = Posting(
            source_id=source_id,
            identity_kind=PostingIdentityKind.NATIVE_REF,
            identity_key=key or uuid.uuid4().hex,
            detail_url=detail_url,
            first_seen_at=min(times),
            last_seen_at=max(times),
        )
        session.add(posting)
        session.flush()
        session.add_all(
            PostingSighting(posting_id=posting.id, fetch_id=f.id, observed_at=f.requested_at)
            for f in fetches
        )
        run = session.scalars(
            select(ExtractionRun).where(ExtractionRun.fetch_id == fetches[0].id)
        ).one()
        session.add_all(
            Assertion(
                posting_id=posting.id,
                fetch_id=fetches[0].id,
                extraction_run_id=run.id,
                field=name,
                value=value,
                method=AssertionMethod.OFFICIAL_API,
                pointer=f"/hits/0/{name}",
                confidence=Decimal("1"),
                observed_at=fetches[0].requested_at,
            )
            for name, value in fields.items()
        )
        session.flush()
        resolve(session, posting.id)
        return posting.id


def _sight(engine: Engine, posting_id: uuid.UUID, fetch_id: uuid.UUID) -> None:
    with Session(engine) as session, session.begin():
        fetch = session.get_one(Fetch, fetch_id)
        session.add(
            PostingSighting(
                posting_id=posting_id, fetch_id=fetch_id, observed_at=fetch.requested_at
            )
        )
        posting = session.get_one(Posting, posting_id)
        posting.last_seen_at = max(posting.last_seen_at, fetch.requested_at)


def _evaluate(engine: Engine, source_id: uuid.UUID, **kw: Any) -> None:
    with Session(engine) as session, session.begin():
        evaluate(session, source_id, now=kw.pop("now", NOW), **kw)


def _state(engine: Engine, posting_id: uuid.UUID) -> tuple[Any, ...]:
    with Session(engine) as session:
        p = session.get_one(Posting, posting_id)
        return (p.state, p.closure_reason, p.disappeared_at)


def _listing(add_target: AddTarget, n: int = 1) -> list[uuid.UUID]:
    return [add_target(url=f"{HOST}/api/jobs?page={i + 1}") for i in range(n)]


# --- pending and active -------------------------------------------------------------------


def test_a_posting_with_a_title_and_an_application_url_becomes_active(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    shown = _fetch(engine, listing, NOW - H)
    complete = _posting(engine, queue_source_id, [shown])
    titled = _posting(engine, queue_source_id, [shown], {"title": "Postdoc"})

    _evaluate(engine, queue_source_id)

    assert _state(engine, complete) == (PostingState.ACTIVE, None, None)
    assert _state(engine, titled) == (PostingState.PENDING, None, None)


# --- absence from the listing -------------------------------------------------------------


def test_missing_three_listing_passes_makes_a_posting_stale(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    shown = _fetch(engine, listing, NOW - 4 * H)
    for hours in (3, 2):
        _fetch(engine, listing, NOW - hours * H)
    posting = _posting(engine, queue_source_id, [shown])
    _evaluate(engine, queue_source_id)
    assert _state(engine, posting)[0] == PostingState.ACTIVE

    _fetch(engine, listing, NOW - H)
    _evaluate(engine, queue_source_id)

    assert _state(engine, posting) == (PostingState.STALE, None, NOW - 3 * H)


def test_a_posting_shown_in_every_pass_stays_active(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    fetches = [_fetch(engine, listing, NOW - hours * H) for hours in (4, 3, 2, 1)]
    posting = _posting(engine, queue_source_id, fetches)

    _evaluate(engine, queue_source_id)

    assert _state(engine, posting) == (PostingState.ACTIVE, None, None)


def test_a_pass_completes_only_when_every_listing_target_was_fetched_again(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    first, second = _listing(add_target, 2)
    shown = _fetch(engine, first, NOW - 10 * H)
    for hours in (9, 7, 5):
        _fetch(engine, first, NOW - hours * H)
    for hours in (8, 6):
        _fetch(engine, second, NOW - hours * H)
    posting = _posting(engine, queue_source_id, [shown])
    _evaluate(engine, queue_source_id)
    assert _state(engine, posting)[0] == PostingState.ACTIVE

    _fetch(engine, second, NOW - 2 * H)
    _evaluate(engine, queue_source_id)

    # The first missed pass completed when the second target was first fetched again.
    assert _state(engine, posting) == (PostingState.STALE, None, NOW - 8 * H)


@pytest.mark.parametrize(
    ("status", "extracted"), [(200, False), (503, True)], ids=["not extracted", "server error"]
)
def test_only_a_successful_extracted_fetch_counts_as_a_pass(
    engine: Engine,
    queue_source_id: uuid.UUID,
    add_target: AddTarget,
    status: int,
    extracted: bool,
) -> None:
    (listing,) = _listing(add_target)
    shown = _fetch(engine, listing, NOW - 4 * H)
    for hours in (3, 2):
        _fetch(engine, listing, NOW - hours * H)
    _fetch(engine, listing, NOW - H, status=status, extracted=extracted)
    posting = _posting(engine, queue_source_id, [shown])

    _evaluate(engine, queue_source_id)

    assert _state(engine, posting)[0] == PostingState.ACTIVE


def test_an_unchanged_listing_fetch_counts_as_a_pass(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    shown = _fetch(engine, listing, NOW - 4 * H)
    for hours in (3, 2, 1):
        _fetch(engine, listing, NOW - hours * H, status=304)
    posting = _posting(engine, queue_source_id, [shown])

    _evaluate(engine, queue_source_id)

    assert _state(engine, posting)[0] == PostingState.STALE


def test_a_posting_stale_for_seven_days_is_closed_as_delisted(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    shown = _fetch(engine, listing, NOW - 10 * D)
    for days in (9, 8):
        _fetch(engine, listing, NOW - days * D)
    _fetch(engine, listing, NOW - 7 * D - H)  # the third missed pass, over 7 days ago
    posting = _posting(engine, queue_source_id, [shown])

    _evaluate(engine, queue_source_id)

    assert _state(engine, posting) == (PostingState.CLOSED, ClosureReason.DELISTED, NOW - 9 * D)


def test_a_posting_stale_for_less_than_seven_days_stays_stale(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    shown = _fetch(engine, listing, NOW - 10 * D)
    for days in (9, 8, 6):
        _fetch(engine, listing, NOW - days * D)
    posting = _posting(engine, queue_source_id, [shown])

    _evaluate(engine, queue_source_id)

    assert _state(engine, posting)[0] == PostingState.STALE


def test_a_stale_posting_listed_again_is_active_with_no_disappearance(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    shown = _fetch(engine, listing, NOW - 5 * H)
    for hours in (4, 3, 2):
        _fetch(engine, listing, NOW - hours * H)
    posting = _posting(engine, queue_source_id, [shown])
    _evaluate(engine, queue_source_id)
    assert _state(engine, posting)[0] == PostingState.STALE

    _sight(engine, posting, _fetch(engine, listing, NOW - H))
    _evaluate(engine, queue_source_id)

    assert _state(engine, posting) == (PostingState.ACTIVE, None, None)


def test_a_detail_page_showing_the_posting_does_not_put_it_back_in_the_listing(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    detail = add_target(url=f"{HOST}/jobs/1", role=TargetRole.DETAIL)
    shown = _fetch(engine, listing, NOW - 5 * H)
    for hours in (4, 3, 2):
        _fetch(engine, listing, NOW - hours * H)
    posting = _posting(engine, queue_source_id, [shown], detail_url=f"{HOST}/jobs/1")

    _sight(engine, posting, _fetch(engine, detail, NOW - H))
    _evaluate(engine, queue_source_id)

    assert _state(engine, posting)[0] == PostingState.STALE


# --- what the source states ---------------------------------------------------------------


def test_a_stated_closed_status_closes_a_listed_posting(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    shown = _fetch(engine, listing, NOW - H)
    posting = _posting(engine, queue_source_id, [shown], {**ACTIVE_FIELDS, "status": "closed"})

    _evaluate(engine, queue_source_id)

    assert _state(engine, posting) == (PostingState.CLOSED, ClosureReason.STATED_CLOSED, None)


@pytest.mark.parametrize(
    ("deadline", "status", "expected"),
    [
        ("2026-10-18", None, (PostingState.CLOSED, ClosureReason.DEADLINE_PASSED)),
        ("2026-10-20", None, (PostingState.ACTIVE, None)),
        ("2026-10-18", "open", (PostingState.ACTIVE, None)),
    ],
    ids=["passed by two days", "passed today", "passed but stated open"],
)
def test_a_deadline_passed_by_more_than_a_day_closes_the_posting(
    engine: Engine,
    queue_source_id: uuid.UUID,
    add_target: AddTarget,
    deadline: str,
    status: str | None,
    expected: tuple[PostingState, ClosureReason | None],
) -> None:
    (listing,) = _listing(add_target)
    shown = _fetch(engine, listing, NOW - H)
    fields = {**ACTIVE_FIELDS, "deadline": deadline} | ({"status": status} if status else {})
    posting = _posting(engine, queue_source_id, [shown], fields)

    _evaluate(engine, queue_source_id)

    assert _state(engine, posting)[:2] == expected


# --- the detail page ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "removed"),
    [
        ([(404, 30), (410, 1)], True),
        ([(404, 5), (404, 1)], False),
        ([(404, 30), (200, 10), (404, 1)], False),
    ],
    ids=["gone a day apart", "gone within a day", "came back in between"],
)
def test_a_detail_page_gone_twice_a_day_apart_removes_the_posting(
    engine: Engine,
    queue_source_id: uuid.UUID,
    add_target: AddTarget,
    statuses: list[tuple[int, int]],
    removed: bool,
) -> None:
    (listing,) = _listing(add_target)
    detail = add_target(url=f"{HOST}/jobs/1", role=TargetRole.DETAIL)
    shown = _fetch(engine, listing, NOW - 40 * H)
    posting = _posting(engine, queue_source_id, [shown], detail_url=f"{HOST}/jobs/1")
    for status, hours_ago in statuses:
        fetch_id = _fetch(engine, detail, NOW - hours_ago * H, status=status)
        if status == 200:
            _sight(engine, posting, fetch_id)

    _evaluate(engine, queue_source_id)

    assert (_state(engine, posting)[0] == PostingState.REMOVED) is removed


def test_a_suspected_soft_404_changes_nothing(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    detail = add_target(url=f"{HOST}/jobs/1", role=TargetRole.DETAIL)
    shown = _fetch(engine, listing, NOW - 5 * H)
    posting = _posting(engine, queue_source_id, [shown], detail_url=f"{HOST}/jobs/1")
    _evaluate(engine, queue_source_id)
    for hours in (4, 3, 2):
        _fetch(engine, listing, NOW - hours * H)
    _fetch(engine, detail, NOW - H)  # 200, extracted, but the posting is not on it

    _evaluate(engine, queue_source_id)

    assert _state(engine, posting) == (PostingState.ACTIVE, None, None)


def test_becoming_stale_makes_the_detail_page_due_now(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    detail = add_target(url=f"{HOST}/jobs/1", role=TargetRole.DETAIL)
    with Session(engine) as session, session.begin():
        session.get_one(CrawlTarget, detail).next_due_at = NOW + 7 * D
    shown = _fetch(engine, listing, NOW - 5 * H)
    for hours in (4, 3, 2):
        _fetch(engine, listing, NOW - hours * H)
    with_page = _posting(engine, queue_source_id, [shown], detail_url=f"{HOST}/jobs/1")
    without_page = _posting(engine, queue_source_id, [shown], detail_url=f"{HOST}/jobs/2")

    _evaluate(engine, queue_source_id)

    assert _state(engine, with_page)[0] == _state(engine, without_page)[0] == PostingState.STALE
    with Session(engine) as session:
        targets = {
            t.url: t
            for t in session.scalars(
                select(CrawlTarget).where(CrawlTarget.role == TargetRole.DETAIL)
            )
        }
        assert targets[f"{HOST}/jobs/1"].next_due_at == NOW
        assert targets[f"{HOST}/jobs/2"].next_due_at <= datetime.now(UTC)


# --- applying changes ---------------------------------------------------------------------


def test_a_state_change_is_resolved_into_the_posting_history(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    posting = _posting(engine, queue_source_id, [_fetch(engine, listing, NOW - H)])

    _evaluate(engine, queue_source_id)
    assert run_once(engine, handlers_for(["resolve"], {}), worker="w1")

    with Session(engine) as session:
        current = session.scalars(
            select(PostingHistory).where(
                PostingHistory.posting_id == posting, PostingHistory.observed_to.is_(None)
            )
        ).one()
        assert current.snapshot["state"] == "active"


def test_an_unchanged_posting_queues_nothing(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    _posting(engine, queue_source_id, [_fetch(engine, listing, NOW - H)])
    _evaluate(engine, queue_source_id)
    run_once(engine, handlers_for(["resolve"], {}), worker="w1")

    _evaluate(engine, queue_source_id)

    with Session(engine) as session:
        assert session.scalars(select(Task.state).where(Task.kind == "resolve")).all() == [
            TaskState.DONE
        ]


def _many_active(engine: Engine, source_id: uuid.UUID, listing: uuid.UUID) -> list[uuid.UUID]:
    """25 active postings; 21 then miss three passes (more than max(15 % of 25, 20) = 20)."""
    old = _fetch(engine, listing, NOW - 5 * H)
    recent = [_fetch(engine, listing, NOW - hours * H) for hours in (4, 3, 2)]
    ids = [_posting(engine, source_id, [old]) for _ in range(21)]
    ids += [_posting(engine, source_id, [old, *recent]) for _ in range(4)]
    with Session(engine) as session, session.begin():
        session.execute(
            update(Posting).where(Posting.id.in_(ids)).values(state=PostingState.ACTIVE)
        )
    return ids


def test_too_many_postings_leaving_active_at_once_are_held(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    ids = _many_active(engine, queue_source_id, listing)

    with pytest.raises(MassClosureHeld):
        _evaluate(engine, queue_source_id)

    assert {_state(engine, i)[0] for i in ids} == {PostingState.ACTIVE}


def test_a_held_mass_closure_is_applied_when_allowed(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    ids = _many_active(engine, queue_source_id, listing)

    _evaluate(engine, queue_source_id, allow_mass_closure=True)

    states = [_state(engine, i)[0] for i in ids]
    assert states.count(PostingState.STALE) == 21


# --- the task and the command -------------------------------------------------------------


def test_the_lifecycle_handler_evaluates_the_tasks_source(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    posting = _posting(engine, queue_source_id, [_fetch(engine, listing, NOW - H)])
    with Session(engine) as session, session.begin():
        session.add(Task(kind="lifecycle", source_id=queue_source_id))

    assert run_once(engine, handlers_for(["lifecycle"], {}), worker="w1")

    assert _state(engine, posting)[0] == PostingState.ACTIVE


def test_a_held_lifecycle_task_fails_with_the_reason_and_changes_nothing(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    (listing,) = _listing(add_target)
    ids = _many_active(engine, queue_source_id, listing)
    with Session(engine) as session, session.begin():
        session.add(Task(kind="lifecycle", source_id=queue_source_id))

    run_once(engine, handlers_for(["lifecycle"], {}), worker="w1")

    with Session(engine) as session:
        task = session.scalars(select(Task).where(Task.kind == "lifecycle")).one()
        assert task.state == TaskState.FAILED
        assert (task.last_error or "").startswith("MassClosureHeld")
    assert {_state(engine, i)[0] for i in ids} == {PostingState.ACTIVE}


def test_the_lifecycle_command_applies_a_held_mass_closure_when_allowed(
    engine: Engine,
    queue_source_id: uuid.UUID,
    add_target: AddTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aoi import cli
    from aoi.db import session as db_session_module

    (listing,) = _listing(add_target)
    ids = _many_active(engine, queue_source_id, listing)
    monkeypatch.setattr(db_session_module, "get_engine", lambda: engine)
    with Session(engine) as session:
        name = session.get_one(Source, queue_source_id).name

    with pytest.raises(SystemExit):
        cli.main(["lifecycle", "--source", name])
    assert {_state(engine, i)[0] for i in ids} == {PostingState.ACTIVE}

    cli.main(["lifecycle", "--source", name, "--allow-mass-closure"])
    assert [_state(engine, i)[0] for i in ids].count(PostingState.STALE) == 21


def _lifecycle_tasks(engine: Engine, source_id: uuid.UUID) -> list[Task]:
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(Task).where(Task.kind == "lifecycle", Task.source_id == source_id)
            )
        )
        session.expunge_all()
        return rows


def _tick(engine: Engine) -> None:
    from aoi.tasks.scheduler import tick

    with Session(engine) as session, session.begin():
        tick(session, lease=timedelta(minutes=10))


def test_the_scheduler_queues_one_lifecycle_task_for_an_eligible_source(
    engine: Engine, queue_source_id: uuid.UUID
) -> None:
    _tick(engine)
    assert _lifecycle_tasks(engine, queue_source_id) == []
    with Session(engine) as session, session.begin():
        session.execute(
            update(Source)
            .where(Source.id == queue_source_id)
            .values(enabled=True, legal_basis=LegalBasis.OFFICIAL_API)
        )

    _tick(engine)
    _tick(engine)

    assert len(_lifecycle_tasks(engine, queue_source_id)) == 1


def test_the_scheduler_queues_lifecycle_again_only_an_hour_after_the_last(
    engine: Engine, queue_source_id: uuid.UUID
) -> None:
    with Session(engine) as session, session.begin():
        session.execute(
            update(Source)
            .where(Source.id == queue_source_id)
            .values(enabled=True, legal_basis=LegalBasis.OFFICIAL_API)
        )
    _tick(engine)
    with Session(engine) as session, session.begin():
        session.execute(update(Task).where(Task.kind == "lifecycle").values(state=TaskState.DONE))
    _tick(engine)
    assert len(_lifecycle_tasks(engine, queue_source_id)) == 1

    with Session(engine) as session, session.begin():
        session.execute(
            update(Task)
            .where(Task.kind == "lifecycle")
            .values(created_at=datetime.now(UTC) - 61 * timedelta(minutes=1))
        )
    _tick(engine)

    assert len(_lifecycle_tasks(engine, queue_source_id)) == 2


def test_a_retired_listing_target_does_not_hold_back_a_pass(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    current, retired = _listing(add_target, 2)
    with Session(engine) as session, session.begin():
        session.get_one(CrawlTarget, retired).retired_at = NOW - 6 * H
    shown = _fetch(engine, current, NOW - 5 * H)
    for hours in (4, 3, 2):
        _fetch(engine, current, NOW - hours * H)
    posting = _posting(engine, queue_source_id, [shown])

    _evaluate(engine, queue_source_id)

    assert _state(engine, posting)[0] == PostingState.STALE
