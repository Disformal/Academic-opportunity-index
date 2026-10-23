"""The resolve step (ADR 0033 §6): a posting's newest value per field, versioned in history.

Per field the newest assertion wins; ties go to the stronger method, then confidence, then id. A
version is written for each observed time whose resolved snapshot differs from the one before, so a
backlog of observations becomes one version per change. Evidence older than the current version
does not rewrite history. explain() ranks each field's values with the same rule.

These tests really commit; the add_target fixture truncates postings and their history afterwards.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from aoi.db.schema.core import Assertion, ExtractionRun, Posting, PostingHistory
from aoi.db.schema.enums import (
    AssertionMethod,
    ClosureReason,
    FetchOutcome,
    PostingIdentityKind,
    PostingState,
)
from aoi.db.schema.evidence import Fetch
from aoi.db.schema.ops import Task
from aoi.tasks.handlers import handlers_for
from aoi.tasks.handlers.resolve import explain, resolve
from aoi.tasks.worker import run_once

pytestmark = pytest.mark.integration

AddTarget = Callable[..., uuid.UUID]
T1 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=1)
T3 = T1 + timedelta(days=2)


@pytest.fixture()
def posting_id(engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget) -> uuid.UUID:
    add_target(url="https://queue-test-resolve.example.org/api/jobs?page=1")
    with Session(engine) as session, session.begin():
        posting = Posting(
            source_id=queue_source_id,
            identity_kind=PostingIdentityKind.NATIVE_REF,
            identity_key="3215677",
            first_seen_at=T1,
            last_seen_at=T1,
        )
        session.add(posting)
        session.flush()
        return posting.id


def _assert(
    engine: Engine,
    posting_id: uuid.UUID,
    field: str,
    value: Any,
    at: datetime,
    *,
    method: AssertionMethod = AssertionMethod.OFFICIAL_API,
    confidence: str = "1",
) -> uuid.UUID:
    """Commit one assertion, with the fetch and extraction run it needs."""
    with Session(engine) as session, session.begin():
        posting = session.get_one(Posting, posting_id)
        target_id = _any_target(session, posting.source_id)
        fetch = Fetch(
            crawl_target_id=target_id,
            url="https://queue-test-resolve.example.org/api/jobs?page=1",
            requested_at=at,
            outcome=FetchOutcome.HIT,
            http_status=200,
        )
        session.add(fetch)
        session.flush()
        run = ExtractionRun(
            fetch_id=fetch.id, extractor="fake-api", extractor_version="1.0.0", started_at=at
        )
        session.add(run)
        session.flush()
        evidence: dict[str, Any] = (
            {"pointer": f"/hits/0/{field}"}
            if method == AssertionMethod.OFFICIAL_API
            else {"span_start": 0, "span_end": 5}
        )
        assertion = Assertion(
            posting_id=posting_id,
            fetch_id=fetch.id,
            extraction_run_id=run.id,
            field=field,
            value=value,
            method=method,
            confidence=Decimal(confidence),
            observed_at=at,
            **evidence,
        )
        session.add(assertion)
        session.flush()
        return assertion.id


def _any_target(session: Session, source_id: uuid.UUID) -> uuid.UUID:
    from aoi.db.schema.ops import CrawlTarget

    return session.scalars(select(CrawlTarget.id).where(CrawlTarget.source_id == source_id)).one()


def _resolve(engine: Engine, posting_id: uuid.UUID) -> None:
    with Session(engine) as session, session.begin():
        resolve(session, posting_id)


def _history(engine: Engine, posting_id: uuid.UUID) -> list[PostingHistory]:
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(PostingHistory)
                .where(PostingHistory.posting_id == posting_id)
                .order_by(PostingHistory.observed_from)
            )
        )
        session.expunge_all()
        return rows


def test_the_first_version_holds_the_newest_value_of_each_field(
    engine: Engine, posting_id: uuid.UUID
) -> None:
    _assert(engine, posting_id, "title", "Postdoc", T1)
    _assert(engine, posting_id, "deadline", "2026-11-01", T1)

    _resolve(engine, posting_id)

    (version,) = _history(engine, posting_id)
    assert version.snapshot == {
        "fields": {"title": "Postdoc", "deadline": "2026-11-01"},
        "state": "pending",
        "closure_reason": None,
    }
    assert (version.observed_from, version.observed_to) == (T1, None)


def test_a_backlog_of_observations_becomes_one_version_per_change(
    engine: Engine, posting_id: uuid.UUID
) -> None:
    _assert(engine, posting_id, "title", "Postdoc", T1)
    _assert(engine, posting_id, "title", "Postdoc", T2)
    _assert(engine, posting_id, "title", "Postdoc in hep-th", T3)

    _resolve(engine, posting_id)

    history = _history(engine, posting_id)
    assert [(v.snapshot["fields"]["title"], v.observed_from, v.observed_to) for v in history] == [
        ("Postdoc", T1, T3),
        ("Postdoc in hep-th", T3, None),
    ]


def test_resolving_again_without_new_evidence_writes_nothing(
    engine: Engine, posting_id: uuid.UUID
) -> None:
    _assert(engine, posting_id, "title", "Postdoc", T1)
    _resolve(engine, posting_id)

    _resolve(engine, posting_id)

    assert len(_history(engine, posting_id)) == 1


def test_a_change_after_the_current_version_closes_it(
    engine: Engine, posting_id: uuid.UUID
) -> None:
    _assert(engine, posting_id, "deadline", "2026-11-01", T1)
    _resolve(engine, posting_id)
    _assert(engine, posting_id, "deadline", "2026-12-01", T2)

    _resolve(engine, posting_id)

    first, second = _history(engine, posting_id)
    assert (first.observed_to, second.observed_from, second.observed_to) == (T2, T2, None)
    assert second.snapshot["fields"] == {"deadline": "2026-12-01"}


def test_evidence_older_than_the_current_version_does_not_rewrite_history(
    engine: Engine, posting_id: uuid.UUID
) -> None:
    _assert(engine, posting_id, "title", "Postdoc", T2)
    _resolve(engine, posting_id)
    _assert(engine, posting_id, "title", "Old title", T1)

    _resolve(engine, posting_id)

    (version,) = _history(engine, posting_id)
    assert version.snapshot["fields"] == {"title": "Postdoc"}


def test_at_the_same_time_the_stronger_method_wins(engine: Engine, posting_id: uuid.UUID) -> None:
    _assert(engine, posting_id, "title", "From the page", T1, method=AssertionMethod.TEMPLATE)
    _assert(engine, posting_id, "title", "From the API", T1)

    _resolve(engine, posting_id)

    (version,) = _history(engine, posting_id)
    assert version.snapshot["fields"]["title"] == "From the API"


def test_with_the_same_time_and_method_the_higher_confidence_wins(
    engine: Engine, posting_id: uuid.UUID
) -> None:
    _assert(engine, posting_id, "title", "Unsure", T1, confidence="0.6")
    _assert(engine, posting_id, "title", "Sure", T1, confidence="0.9")

    _resolve(engine, posting_id)

    (version,) = _history(engine, posting_id)
    assert version.snapshot["fields"]["title"] == "Sure"


def test_the_stated_dates_are_the_resolved_deadline_and_posting_date(
    engine: Engine, posting_id: uuid.UUID
) -> None:
    _assert(engine, posting_id, "deadline", "2026-11-01", T1)
    _assert(engine, posting_id, "posted_at", "2026-10-16", T1)

    _resolve(engine, posting_id)

    (version,) = _history(engine, posting_id)
    assert version.stated_deadline_at == datetime(2026, 11, 1, tzinfo=UTC)
    assert version.stated_posted_at == datetime(2026, 10, 16, tzinfo=UTC)


@pytest.mark.parametrize("stated", ["2010-01", "2011"])
def test_a_date_stated_only_to_the_month_or_year_leaves_the_stated_column_empty(
    engine: Engine, posting_id: uuid.UUID, stated: str
) -> None:
    # Real INSPIRE archive values (deadline_date of jobs from 2010-2019).
    _assert(engine, posting_id, "deadline", stated, T1)
    _assert(engine, posting_id, "posted_at", stated, T1)

    _resolve(engine, posting_id)

    (version,) = _history(engine, posting_id)
    assert (version.stated_deadline_at, version.stated_posted_at) == (None, None)
    assert version.snapshot["fields"]["deadline"] == stated


@pytest.mark.parametrize("stated", ["8888-12-31", "1111-01-01"])
def test_a_placeholder_date_leaves_the_stated_column_empty(
    engine: Engine, posting_id: uuid.UUID, stated: str
) -> None:
    # INSPIRE archive jobs state "no deadline" as 8888 or 1111 (8888-12-31 is a real value).
    _assert(engine, posting_id, "deadline", stated, T1)

    _resolve(engine, posting_id)

    (version,) = _history(engine, posting_id)
    assert version.stated_deadline_at is None
    assert version.snapshot["fields"]["deadline"] == stated


def test_the_placeholder_migration_clears_only_placeholder_stated_times_and_reverses(
    engine: Engine, posting_id: uuid.UUID, migrate: Callable[..., None]
) -> None:
    def version(at: datetime, deadline: str, stated: datetime | None) -> PostingHistory:
        snapshot = {"fields": {"deadline": deadline}, "state": "closed", "closure_reason": None}
        return PostingHistory(
            posting_id=posting_id,
            observed_from=at,
            observed_to=T2 if at == T1 else None,
            stated_deadline_at=stated,
            snapshot=snapshot,
        )

    real = datetime(2026, 11, 1, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(version(T1, "2026-11-01", real))
        session.add(version(T2, "8888-12-31", None))  # as resolved under ADR 0036

    def stated() -> list[datetime | None]:
        return [v.stated_deadline_at for v in _history(engine, posting_id)]

    try:
        migrate("downgrade", "ff6e6f4fa217")
        assert stated() == [real, datetime(8888, 12, 31, tzinfo=UTC)]
    finally:
        migrate("upgrade", "head")
    assert stated() == [real, None]


def test_a_new_version_carries_the_lifecycle_state_of_the_one_before(
    engine: Engine, posting_id: uuid.UUID
) -> None:
    _assert(engine, posting_id, "title", "Postdoc", T1)
    # The database lets a posting be active only with these two fields (provenance trigger).
    _assert(engine, posting_id, "application_url", "https://example.org/apply", T1)
    _resolve(engine, posting_id)
    with Session(engine) as session, session.begin():
        version = session.scalars(
            select(PostingHistory).where(PostingHistory.posting_id == posting_id)
        ).one()
        version.snapshot = {**version.snapshot, "state": "active"}
        session.get_one(Posting, posting_id).state = PostingState.ACTIVE
    _assert(engine, posting_id, "title", "Postdoc in hep-th", T2)

    _resolve(engine, posting_id)

    assert _history(engine, posting_id)[-1].snapshot["state"] == "active"


def test_explain_ranks_each_fields_values_with_the_winner_first(
    engine: Engine, posting_id: uuid.UUID
) -> None:
    old = _assert(engine, posting_id, "title", "Postdoc", T1)
    new = _assert(engine, posting_id, "title", "Postdoc in hep-th", T2)
    deadline = _assert(engine, posting_id, "deadline", "2026-11-01", T1)

    with Session(engine) as session:
        report = explain(session, posting_id)

    assert [(c["assertion_id"], c["value"]) for c in report["title"]] == [
        (new, "Postdoc in hep-th"),
        (old, "Postdoc"),
    ]
    assert report["title"][0]["pointer"] == "/hits/0/title"
    assert report["title"][0]["observed_at"] == T2
    assert [c["assertion_id"] for c in report["deadline"]] == [deadline]


def test_the_resolve_handler_resolves_the_tasks_posting(
    engine: Engine, queue_source_id: uuid.UUID, posting_id: uuid.UUID
) -> None:
    _assert(engine, posting_id, "title", "Postdoc", T1)
    with Session(engine) as session, session.begin():
        session.add(
            Task(
                kind="resolve", source_id=queue_source_id, payload={"posting_id": str(posting_id)}
            )
        )

    assert run_once(engine, handlers_for(["resolve"], {}), worker="w1")

    assert len(_history(engine, posting_id)) == 1


def test_a_lifecycle_change_is_versioned_when_it_is_resolved(
    engine: Engine, posting_id: uuid.UUID
) -> None:
    _assert(engine, posting_id, "title", "Postdoc", T1)
    _resolve(engine, posting_id)
    with Session(engine) as session, session.begin():
        posting = session.get_one(Posting, posting_id)
        posting.state = PostingState.CLOSED
        posting.closure_reason = ClosureReason.STATED_CLOSED
    before = datetime.now(UTC)

    _resolve(engine, posting_id)

    first, second = _history(engine, posting_id)
    assert second.snapshot == {
        "fields": {"title": "Postdoc"},
        "state": "closed",
        "closure_reason": "stated_closed",
    }
    assert before <= second.observed_from <= datetime.now(UTC)
    assert first.observed_to == second.observed_from


def test_older_evidence_that_changes_the_values_is_versioned_when_it_is_resolved(
    engine: Engine, posting_id: uuid.UUID
) -> None:
    _assert(engine, posting_id, "title", "Postdoc", T2)
    _resolve(engine, posting_id)
    _assert(engine, posting_id, "deadline", "2026-11-01", T1)  # extracted late
    before = datetime.now(UTC)

    _resolve(engine, posting_id)

    first, second = _history(engine, posting_id)
    assert first.snapshot["fields"] == {"title": "Postdoc"}
    assert second.snapshot["fields"] == {"title": "Postdoc", "deadline": "2026-11-01"}
    assert second.observed_from >= before
