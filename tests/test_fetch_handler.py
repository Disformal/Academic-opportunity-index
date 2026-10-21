"""The fetch handler (plan §4.3, ADR 0030): one request for one crawl target.

The WARC object is stored before the immutable evidence.fetch row (ADR 0027). An unchanged body is
stored as a revisit; it and a 304 are queued for extraction too, which only records sightings
(ADR 0033). The recrawl interval halves on change and doubles otherwise, within [1 h, 7 d].
Failed requests are recorded as fetch rows and fail the attempt.

fetch() (the network) is replaced; the database, the object store and the worker are real. These
tests really commit.
"""

import hashlib
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from aoi.db.schema.enums import FetchOutcome, TaskState
from aoi.db.schema.evidence import Fetch
from aoi.db.schema.ops import CrawlTarget, Host, Task
from aoi.pipeline.canonicalize import BlockedAddress, ResolutionFailed
from aoi.pipeline.fetch import FetchResult
from aoi.storage.warc import read_capture
from aoi.tasks.handlers import fetch as fetch_handler
from aoi.tasks.worker import run_once

pytestmark = pytest.mark.integration

AddTasks = Callable[..., list[uuid.UUID]]
AddHost = Callable[..., uuid.UUID]

URL = "https://inspirehep.net/api/jobs?page=1"
BODY = b'{"hits": [{"id": 2911234, "title": "Postdoc"}], "next": null}'


@pytest.fixture()
def target_id(add_target: Callable[..., uuid.UUID]) -> uuid.UUID:
    return add_target(url=URL)


class FakeNetwork:
    """Stands in for aoi.pipeline.fetch.fetch: returns the queued results and records calls."""

    def __init__(self) -> None:
        self.results: list[FetchResult | Exception] = []
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append({"url": url, **kwargs})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture()
def network(monkeypatch: pytest.MonkeyPatch) -> FakeNetwork:
    fake = FakeNetwork()
    monkeypatch.setattr(fetch_handler, "fetch", fake)
    return fake


def _result(
    status: int = 200, body: bytes = BODY, headers: dict[str, str] | None = None
) -> FetchResult:
    return FetchResult(
        url=URL,
        final_url="https://inspirehep.net/api/jobs?page=1&size=25",
        status=status,
        headers={"content-type": "application/json", **(headers or {})},
        body=body,
        elapsed_ms=140,
        not_modified=status == 304,
        resolved_ip="188.185.0.1",
        http_version="HTTP/2",
        request_headers={"host": "inspirehep.net", "user-agent": "aoi/0.1"},
    )


def _fetch_once(engine: Engine, queue_source_id: uuid.UUID, target_id: uuid.UUID) -> uuid.UUID:
    with Session(engine) as session, session.begin():
        host_id = session.get_one(CrawlTarget, target_id).host_id
        task = Task(
            kind="fetch",
            source_id=queue_source_id,
            host_id=host_id,
            crawl_target_id=target_id,
        )
        session.add(task)
        session.flush()
        task_id = task.id
        # The previous fetch moved the host 2 ms ahead; tests do not wait for it.
        session.get_one(Host, host_id).next_allowed_at = session.get_one(Host, host_id).created_at
    assert run_once(engine, {"fetch": fetch_handler.handle_fetch}, worker="w1")
    return task_id


def _fetches(engine: Engine) -> list[Fetch]:
    with Session(engine) as session:
        rows = list(session.scalars(select(Fetch).order_by(Fetch.requested_at)))
        session.expunge_all()
        return rows


def _extract_tasks(engine: Engine) -> list[Task]:
    with Session(engine) as session:
        rows = list(
            session.scalars(select(Task).where(Task.kind == "extract").order_by(Task.created_at))
        )
        session.expunge_all()
        return rows


def _target(engine: Engine, target_id: uuid.UUID) -> CrawlTarget:
    with Session(engine) as session:
        target = session.get_one(CrawlTarget, target_id)
        session.expunge(target)
        return target


def test_a_first_fetch_stores_evidence_records_the_fetch_and_queues_extraction(
    engine: Engine, queue_source_id: uuid.UUID, target_id: uuid.UUID, network: FakeNetwork
) -> None:
    network.results = [
        _result(headers={"etag": '"v1"', "last-modified": "Sat, 17 Oct 2026 10:00:00 GMT"})
    ]

    task_id = _fetch_once(engine, queue_source_id, target_id)

    (row,) = _fetches(engine)
    assert row.outcome == FetchOutcome.HIT
    assert row.url == URL
    assert row.final_url == "https://inspirehep.net/api/jobs?page=1&size=25"
    assert row.http_status == 200
    assert str(row.resolved_ip) == "188.185.0.1"
    assert row.body_sha256 == hashlib.sha256(BODY).hexdigest()
    assert row.body_bytes == len(BODY)
    assert row.warc_key is not None
    capture = read_capture(row.warc_key)
    assert capture.record_type == "response"
    assert capture.record_id == row.warc_record_id
    assert capture.body == BODY

    target = _target(engine, target_id)
    assert target.etag == '"v1"'
    assert target.last_modified == "Sat, 17 Oct 2026 10:00:00 GMT"
    assert target.content_sha256 == row.body_sha256

    (extract,) = _extract_tasks(engine)
    assert extract.payload == {"fetch_id": str(row.id)}
    with Session(engine) as session:
        assert session.get_one(Task, task_id).state == TaskState.DONE


def test_the_next_fetch_is_conditional_on_the_stored_validators(
    engine: Engine, queue_source_id: uuid.UUID, target_id: uuid.UUID, network: FakeNetwork
) -> None:
    network.results = [_result(headers={"etag": '"v1"'}), _result(304, b"")]

    _fetch_once(engine, queue_source_id, target_id)
    _fetch_once(engine, queue_source_id, target_id)

    assert network.calls[0]["etag"] is None
    assert network.calls[1]["etag"] == '"v1"'
    second = _fetches(engine)[1]
    assert second.outcome == FetchOutcome.NOT_MODIFIED
    assert second.http_status == 304
    assert len(_extract_tasks(engine)) == 2


def test_an_unchanged_body_is_a_revisit_and_is_queued_for_sighting(
    engine: Engine, queue_source_id: uuid.UUID, target_id: uuid.UUID, network: FakeNetwork
) -> None:
    network.results = [_result(), _result()]

    _fetch_once(engine, queue_source_id, target_id)
    _fetch_once(engine, queue_source_id, target_id)

    first, second = _fetches(engine)
    assert second.warc_key is not None
    capture = read_capture(second.warc_key)
    assert capture.record_type == "revisit"
    assert capture.refers_to == first.warc_record_id
    assert [t.payload["fetch_id"] for t in _extract_tasks(engine)] == [
        str(first.id),
        str(second.id),
    ]


def test_the_recrawl_interval_doubles_when_unchanged_and_halves_on_change(
    engine: Engine, queue_source_id: uuid.UUID, target_id: uuid.UUID, network: FakeNetwork
) -> None:
    network.results = [_result(), _result(), _result(body=BODY + b" ")]
    start = _target(engine, target_id).recrawl_interval

    _fetch_once(engine, queue_source_id, target_id)  # first sight counts as a change
    after_first = _target(engine, target_id)
    _fetch_once(engine, queue_source_id, target_id)
    after_unchanged = _target(engine, target_id)
    _fetch_once(engine, queue_source_id, target_id)
    after_change = _target(engine, target_id)

    assert after_first.recrawl_interval == start / 2
    assert after_unchanged.recrawl_interval == start
    assert after_change.recrawl_interval == start / 2
    assert after_change.next_due_at - after_change.updated_at == start / 2
    assert len(_extract_tasks(engine)) == 3  # unchanged fetches are extracted for sightings


@pytest.mark.parametrize(
    ("interval", "changed", "expected"),
    [
        (timedelta(hours=1), True, timedelta(hours=1)),
        (timedelta(days=7), False, timedelta(days=7)),
        (timedelta(days=5), False, timedelta(days=7)),
        (timedelta(minutes=90), True, timedelta(hours=1)),
    ],
)
def test_the_recrawl_interval_stays_within_one_hour_and_seven_days(
    interval: timedelta, changed: bool, expected: timedelta
) -> None:
    assert fetch_handler.next_interval(interval, changed=changed) == expected


@pytest.mark.parametrize(
    ("status", "outcome"), [(503, FetchOutcome.ERROR), (429, FetchOutcome.RATE_LIMITED)]
)
def test_a_server_error_is_recorded_with_its_evidence_and_fails_the_attempt(
    engine: Engine,
    queue_source_id: uuid.UUID,
    target_id: uuid.UUID,
    network: FakeNetwork,
    status: int,
    outcome: FetchOutcome,
) -> None:
    network.results = [_result(status, b"<h1>Try again later</h1>")]

    task_id = _fetch_once(engine, queue_source_id, target_id)

    (row,) = _fetches(engine)
    assert row.outcome == outcome
    assert row.http_status == status
    assert row.warc_key is not None
    assert read_capture(row.warc_key).status == status
    assert _target(engine, target_id).content_sha256 is None
    assert _extract_tasks(engine) == []
    with Session(engine) as session:
        task = session.get_one(Task, task_id)
        assert task.state == TaskState.FAILED
        assert task.last_error is not None
        assert str(status) in task.last_error
        assert session.get_one(Host, task.host_id).consecutive_failures == 1


def test_a_429_is_not_waited_out_in_the_worker(
    engine: Engine, queue_source_id: uuid.UUID, target_id: uuid.UUID, network: FakeNetwork
) -> None:
    """The queue's backoff handles it; sleeping in fetch() could outlast the lease (ADR 0030)."""
    network.results = [_result(429, b"<h1>Too Many Requests</h1>")]

    _fetch_once(engine, queue_source_id, target_id)

    assert network.calls[0]["max_429_retries"] == 0


def test_a_missing_page_is_a_completed_fetch_not_a_failure(
    engine: Engine, queue_source_id: uuid.UUID, target_id: uuid.UUID, network: FakeNetwork
) -> None:
    network.results = [_result(404, b"<h1>Not Found</h1>")]

    task_id = _fetch_once(engine, queue_source_id, target_id)

    (row,) = _fetches(engine)
    assert row.outcome == FetchOutcome.HIT
    assert row.http_status == 404
    assert _extract_tasks(engine) == []
    with Session(engine) as session:
        assert session.get_one(Task, task_id).state == TaskState.DONE


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (httpx.ConnectTimeout("timed out"), FetchOutcome.ERROR),
        (BlockedAddress("10.0.0.1 is not globally routable"), FetchOutcome.BLOCKED),
        (ResolutionFailed("DNS resolution failed for 'inspirehep.net'"), FetchOutcome.ERROR),
    ],
)
def test_a_request_that_got_no_response_is_recorded_without_evidence(
    engine: Engine,
    queue_source_id: uuid.UUID,
    target_id: uuid.UUID,
    network: FakeNetwork,
    error: Exception,
    outcome: FetchOutcome,
) -> None:
    network.results = [error]

    task_id = _fetch_once(engine, queue_source_id, target_id)

    (row,) = _fetches(engine)
    assert row.outcome == outcome
    assert row.http_status is None
    assert row.warc_key is None
    assert row.error is not None
    assert type(error).__name__ in row.error
    with Session(engine) as session:
        assert session.get_one(Task, task_id).state == TaskState.FAILED
