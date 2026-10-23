"""The extract handler (plan §4.3, ADR 0030, 0031): one fetch's WARC through its source's adapter.

parse_listing runs on listing and feed targets (its targets become crawl targets, its refs update
last_seen); parse_detail runs on every document. Postings are found or created by within-source
identity (ADR 0008); assertions carry the fetch, the extraction run and their evidence pointer.
Every posting a document shows is sighted once per fetch (ADR 0033). One extraction per fetch per
extractor version (ADR 0030 §2). An unchanged fetch (a revisit, or a 304) re-parses the earlier
content for sightings only: no assertions.

Replay (ADR 0006) queues an extract task for every stored 2xx or 304 response of a source that the
current adapter version has not extracted yet.

The database, the object store and the worker are real; these tests really commit.
"""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fake_adapter import FakeApiAdapter
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from aoi.adapters.protocol import Document, ParsedPosting, PostingRef
from aoi.db.schema.core import Assertion, ExtractionRun, Posting, PostingSighting, Source
from aoi.db.schema.enums import (
    AssertionMethod,
    FetchOutcome,
    OpportunityKind,
    PostingIdentityKind,
    PostingState,
    TargetRole,
    TaskState,
)
from aoi.db.schema.evidence import Fetch
from aoi.db.schema.ops import CrawlTarget, Host, Task
from aoi.storage.warc import EarlierCapture, Exchange, store_exchange
from aoi.tasks.handlers.extract import make_extract_handler
from aoi.tasks.replay import queue_replay
from aoi.tasks.worker import run_once

pytestmark = pytest.mark.integration

AddTarget = Callable[..., uuid.UUID]

HOST = "queue-test-extract.example.org"
LISTING_URL = f"https://{HOST}/api/jobs?page=1"
FIRST_SEEN = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)


def _page(*ids: int, next_url: str | None = f"https://{HOST}/api/jobs?page=2") -> bytes:
    hits = [{"id": i, "title": f"Postdoc {i}", "url": f"https://{HOST}/jobs/{i}"} for i in ids]
    return json.dumps({"hits": hits, "next": next_url}).encode()


def _stored_fetch(
    engine: Engine,
    target_id: uuid.UUID,
    body: bytes,
    at: datetime = FIRST_SEEN,
    *,
    status: int = 200,
    same_as: EarlierCapture | None = None,
    queue_extract: bool = True,
) -> uuid.UUID:
    """A committed fetch of the target with its WARC, and (by default) an extract task for it."""
    fetch_id = uuid.uuid4()
    with Session(engine) as session, session.begin():
        target = session.get_one(CrawlTarget, target_id)
        stored = store_exchange(
            fetch_id,
            Exchange(
                url=target.url,
                request_headers={"host": HOST},
                http_version="HTTP/2",
                status=status,
                response_headers={"content-type": "application/json"},
                body=body,
                fetched_at=at,
            ),
            same_as=same_as,
        )
        session.add(
            Fetch(
                id=fetch_id,
                crawl_target_id=target_id,
                url=target.url,
                final_url=target.url,
                requested_at=at,
                outcome=FetchOutcome.HIT,
                http_status=status,
                warc_record_id=stored.record_id,
                warc_key=stored.key,
            )
        )
        if queue_extract:
            session.add(
                Task(
                    kind="extract",
                    source_id=target.source_id,
                    payload={"fetch_id": str(fetch_id)},
                )
            )
    return fetch_id


def _capture_of(engine: Engine, fetch_id: uuid.UUID) -> EarlierCapture:
    with Session(engine) as session:
        fetch = session.get_one(Fetch, fetch_id)
        return EarlierCapture(str(fetch.warc_record_id), fetch.url, fetch.requested_at)


def _extract(engine: Engine, source_id: uuid.UUID, adapter: FakeApiAdapter | None = None) -> None:
    with Session(engine) as session:
        name = session.get_one(Source, source_id).name
    handler = make_extract_handler({name: adapter or FakeApiAdapter()})
    assert run_once(engine, {"extract": handler}, worker="w1")


def _postings(engine: Engine) -> list[Posting]:
    with Session(engine) as session:
        rows = list(session.scalars(select(Posting).order_by(Posting.identity_key)))
        session.expunge_all()
        return rows


def _count(engine: Engine, model: type) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def _sightings(engine: Engine) -> list[tuple[str, uuid.UUID, datetime]]:
    """(identity key, fetch, observed time) of every sighting, in order."""
    with Session(engine) as session:
        rows = session.execute(
            select(Posting.identity_key, PostingSighting.fetch_id, PostingSighting.observed_at)
            .join(Posting, Posting.id == PostingSighting.posting_id)
            .order_by(PostingSighting.observed_at, Posting.identity_key)
        ).all()
        return [tuple(row) for row in rows]  # type: ignore[misc]


def test_a_listing_document_creates_its_postings_with_their_assertions(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    fetch_id = _stored_fetch(engine, target_id, _page(2911234, 2911240))

    _extract(engine, queue_source_id)

    postings = _postings(engine)
    assert [(p.identity_kind, p.identity_key) for p in postings] == [
        (PostingIdentityKind.NATIVE_REF, "2911234"),
        (PostingIdentityKind.NATIVE_REF, "2911240"),
    ]
    assert all(p.source_id == queue_source_id for p in postings)
    assert all(p.state == PostingState.PENDING for p in postings)
    assert all(p.first_seen_at == p.last_seen_at == FIRST_SEEN for p in postings)
    assert postings[0].detail_url == f"https://{HOST}/jobs/2911234"

    with Session(engine) as session:
        (run,) = session.scalars(select(ExtractionRun)).all()
        assert (run.fetch_id, run.extractor, run.extractor_version) == (
            fetch_id,
            "fake-api",
            "1.0.0",
        )
        assert run.finished_at is not None
        assertions = session.scalars(select(Assertion).order_by(Assertion.pointer)).all()
        assert [(a.field, a.value, a.pointer) for a in assertions] == [
            ("title", "Postdoc 2911234", "/hits/0/title"),
            ("title", "Postdoc 2911240", "/hits/1/title"),
        ]
        assert all(a.fetch_id == fetch_id and a.extraction_run_id == run.id for a in assertions)
        assert all(a.method == AssertionMethod.OFFICIAL_API for a in assertions)
        assert all(a.observed_at == FIRST_SEEN for a in assertions)


def test_each_posting_a_document_shows_is_sighted_once_for_the_fetch(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    # The fake adapter shows each hit twice: as a listing ref and as a parsed posting.
    fetch_id = _stored_fetch(engine, target_id, _page(2911234, 2911240))

    _extract(engine, queue_source_id)

    assert _sightings(engine) == [
        ("2911234", fetch_id, FIRST_SEEN),
        ("2911240", fetch_id, FIRST_SEEN),
    ]


def test_a_listing_adds_the_pages_it_links_to_as_crawl_targets(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    _stored_fetch(engine, target_id, _page(2911234))

    _extract(engine, queue_source_id)

    with Session(engine) as session:
        new = session.scalars(select(CrawlTarget).where(CrawlTarget.id != target_id)).one()
        assert new.url == f"https://{HOST}/api/jobs?page=2"
        assert new.role == TargetRole.LISTING
        assert new.source_id == queue_source_id
        assert session.get_one(Host, new.host_id).hostname == HOST
        assert new.next_due_at <= datetime.now(UTC)


def test_a_link_to_an_existing_target_does_not_duplicate_it(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    _stored_fetch(engine, target_id, _page(2911234, next_url=LISTING_URL))

    _extract(engine, queue_source_id)

    assert _count(engine, CrawlTarget) == 1


def test_a_stored_page_of_a_retired_target_adds_no_targets_but_still_records_its_postings(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    fetch_id = _stored_fetch(engine, target_id, _page(2911234))
    with Session(engine) as session, session.begin():
        session.get_one(CrawlTarget, target_id).retired_at = FIRST_SEEN + timedelta(hours=1)

    _extract(engine, queue_source_id)

    assert _count(engine, CrawlTarget) == 1
    assert _sightings(engine) == [("2911234", fetch_id, FIRST_SEEN)]
    assert _count(engine, Assertion) == 1


def test_a_detail_document_is_not_parsed_as_a_listing(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=f"https://{HOST}/api/jobs/2911234", role=TargetRole.DETAIL)
    _stored_fetch(engine, target_id, _page(2911234))

    _extract(engine, queue_source_id)

    assert _count(engine, CrawlTarget) == 1
    assert len(_postings(engine)) == 1


def test_a_posting_seen_again_keeps_its_first_sighting_and_moves_its_last(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    later = FIRST_SEEN + timedelta(days=1)
    _stored_fetch(engine, target_id, _page(2911234))
    _extract(engine, queue_source_id)
    _stored_fetch(engine, target_id, _page(2911234, 2911240), at=later)

    _extract(engine, queue_source_id)

    seen = {p.identity_key: (p.first_seen_at, p.last_seen_at) for p in _postings(engine)}
    assert seen == {"2911234": (FIRST_SEEN, later), "2911240": (later, later)}
    assert _count(engine, Assertion) == 3


def test_an_older_document_does_not_move_last_seen_backwards(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    later = FIRST_SEEN + timedelta(days=1)
    _stored_fetch(engine, target_id, _page(2911234), at=later)
    _extract(engine, queue_source_id)
    _stored_fetch(engine, target_id, _page(2911234), at=FIRST_SEEN)

    _extract(engine, queue_source_id)

    (posting,) = _postings(engine)
    assert (posting.first_seen_at, posting.last_seen_at) == (FIRST_SEEN, later)


def test_extracting_a_fetch_again_with_the_same_version_writes_nothing(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    fetch_id = _stored_fetch(engine, target_id, _page(2911234))
    with Session(engine) as session, session.begin():
        session.add(
            Task(kind="extract", source_id=queue_source_id, payload={"fetch_id": str(fetch_id)})
        )

    _extract(engine, queue_source_id)
    _extract(engine, queue_source_id)

    assert _count(engine, ExtractionRun) == 1
    assert _count(engine, Assertion) == 1
    with Session(engine) as session:
        states = session.scalars(select(Task.state).where(Task.kind == "extract")).all()
    assert states == [TaskState.DONE, TaskState.DONE]


class DetailUrlAdapter(FakeApiAdapter):
    """Identifies postings by detail URL, spelled differently in each hit, and knows the kind."""

    def parse_detail(self, doc: Document) -> list[ParsedPosting]:
        return [
            ParsedPosting(
                ref=PostingRef(
                    PostingIdentityKind.DETAIL_URL, hit["url"], hit["url"], OpportunityKind.JOB
                ),
                assertions=(),
            )
            for hit in json.loads(doc.body)["hits"]
        ]


def test_postings_identified_by_detail_url_use_its_canonical_form(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=f"https://{HOST}/jobs/42", role=TargetRole.DETAIL)
    spellings = [
        f"https://{HOST.upper()}/jobs/42/?utm_source=newsletter",
        f"https://{HOST}:443/jobs/42",
    ]
    body = json.dumps({"hits": [{"id": 42, "url": u} for u in spellings], "next": None})
    _stored_fetch(engine, target_id, body.encode())
    with Session(engine) as session:
        name = session.get_one(Source, queue_source_id).name

    handler = make_extract_handler({name: DetailUrlAdapter()})
    assert run_once(engine, {"extract": handler}, worker="w1")

    (posting,) = _postings(engine)
    assert posting.identity_key == f"https://{HOST}/jobs/42"
    assert posting.detail_url == f"https://{HOST}/jobs/42"
    assert posting.kind == OpportunityKind.JOB


class FakeApiAdapterV2(FakeApiAdapter):
    version = "2.0.0"


def test_a_revisit_sights_the_earlier_contents_postings_without_assertions(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    later = FIRST_SEEN + timedelta(days=1)
    first = _stored_fetch(engine, target_id, _page(2911234))
    _extract(engine, queue_source_id)
    revisit = _stored_fetch(
        engine, target_id, _page(2911234), later, same_as=_capture_of(engine, first)
    )

    _extract(engine, queue_source_id)

    assert _sightings(engine) == [
        ("2911234", first, FIRST_SEEN),
        ("2911234", revisit, later),
    ]
    assert _count(engine, Assertion) == 1
    assert _count(engine, CrawlTarget) == 2  # the listing and its page 2, not added twice
    (posting,) = _postings(engine)
    assert posting.last_seen_at == later
    with Session(engine) as session:
        run = session.scalars(select(ExtractionRun).where(ExtractionRun.fetch_id == revisit)).one()
        assert run.finished_at is not None


def test_a_revisit_is_sighted_even_when_its_earlier_content_was_never_extracted(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    later = FIRST_SEEN + timedelta(days=1)
    first = _stored_fetch(engine, target_id, _page(2911234), queue_extract=False)
    revisit = _stored_fetch(
        engine, target_id, _page(2911234), later, same_as=_capture_of(engine, first)
    )

    _extract(engine, queue_source_id)

    assert _sightings(engine) == [("2911234", revisit, later)]
    assert _count(engine, Assertion) == 0


def test_a_304_sights_the_postings_of_the_targets_latest_earlier_response(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    day = timedelta(days=1)
    _stored_fetch(engine, target_id, _page(2911234), queue_extract=False)
    _stored_fetch(engine, target_id, _page(2911240), FIRST_SEEN + day, queue_extract=False)
    not_modified = _stored_fetch(engine, target_id, b"", FIRST_SEEN + 2 * day, status=304)

    _extract(engine, queue_source_id)

    assert _sightings(engine) == [("2911240", not_modified, FIRST_SEEN + 2 * day)]
    assert _count(engine, Assertion) == 0


def _replay(engine: Engine, source_id: uuid.UUID, adapter: FakeApiAdapter) -> list[uuid.UUID]:
    """Queue a replay; return the fetch ids of the extract tasks it queued."""
    with Session(engine) as session, session.begin():
        before = set(session.scalars(select(Task.id)))
        queue_replay(session, session.get_one(Source, source_id), adapter)
    with Session(engine) as session:
        tasks = session.scalars(select(Task).where(Task.id.not_in(before))).all()
        assert all(t.kind == "extract" and t.source_id == source_id for t in tasks)
        return sorted(uuid.UUID(t.payload["fetch_id"]) for t in tasks)


def _stored_history(engine: Engine, target_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """An extracted response, a newer unextracted one, a 304 and a 503 (no extract tasks)."""
    day = timedelta(days=1)
    return {
        "extracted": _stored_fetch(engine, target_id, _page(1), queue_extract=False),
        "new": _stored_fetch(engine, target_id, _page(2), FIRST_SEEN + day, queue_extract=False),
        "304": _stored_fetch(
            engine, target_id, b"", FIRST_SEEN + 2 * day, status=304, queue_extract=False
        ),
        "503": _stored_fetch(
            engine, target_id, b"busy", FIRST_SEEN + 3 * day, status=503, queue_extract=False
        ),
    }


def test_replay_queues_the_stored_responses_this_version_has_not_extracted(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    fetches = _stored_history(engine, add_target(url=LISTING_URL))
    with Session(engine) as session, session.begin():
        session.add(
            Task(
                kind="extract",
                source_id=queue_source_id,
                payload={"fetch_id": str(fetches["extracted"])},
            )
        )
    _extract(engine, queue_source_id)

    assert _replay(engine, queue_source_id, FakeApiAdapter()) == sorted(
        [fetches["new"], fetches["304"]]
    )


def test_replay_with_a_new_adapter_version_queues_every_stored_response(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    fetches = _stored_history(engine, add_target(url=LISTING_URL))

    queued = _replay(engine, queue_source_id, FakeApiAdapterV2())

    assert queued == sorted([fetches["extracted"], fetches["new"], fetches["304"]])


def test_a_replayed_fetch_is_extracted_by_the_new_version(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    fetch_id = _stored_fetch(engine, target_id, _page(2911234))
    _extract(engine, queue_source_id)

    _replay(engine, queue_source_id, FakeApiAdapterV2())
    _extract(engine, queue_source_id, FakeApiAdapterV2())

    with Session(engine) as session:
        runs = session.scalars(select(ExtractionRun).order_by(ExtractionRun.extractor_version))
        assert [(r.fetch_id, r.extractor_version) for r in runs] == [
            (fetch_id, "1.0.0"),
            (fetch_id, "2.0.0"),
        ]
        assert _count(engine, Assertion) == 2
    assert len(_postings(engine)) == 1
    assert len(_sightings(engine)) == 1


def test_the_replay_command_queues_the_sources_stored_responses(
    engine: Engine,
    queue_source_id: uuid.UUID,
    add_target: AddTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aoi import cli
    from aoi.adapters import registry
    from aoi.db import session as db_session_module

    fetch_id = _stored_fetch(engine, add_target(url=LISTING_URL), _page(1), queue_extract=False)
    with Session(engine) as session:
        name = session.get_one(Source, queue_source_id).name
    monkeypatch.setitem(registry.ADAPTERS, name, FakeApiAdapter())
    monkeypatch.setattr(db_session_module, "get_engine", lambda: engine)

    cli.main(["replay", "--source", name])

    with Session(engine) as session:
        (task,) = session.scalars(select(Task)).all()
        assert task.payload == {"fetch_id": str(fetch_id)}


def test_the_replay_command_refuses_an_unknown_source(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aoi import cli
    from aoi.db import session as db_session_module

    monkeypatch.setattr(db_session_module, "get_engine", lambda: engine)

    with pytest.raises(SystemExit, match="no source named 'nonexistent'"):
        cli.main(["replay", "--source", "nonexistent"])


def test_the_replay_command_refuses_a_source_without_an_adapter(
    engine: Engine, queue_source_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aoi import cli
    from aoi.db import session as db_session_module

    with Session(engine) as session:
        name = session.get_one(Source, queue_source_id).name
    monkeypatch.setattr(db_session_module, "get_engine", lambda: engine)

    with pytest.raises(SystemExit, match="no adapter registered"):
        cli.main(["replay", "--source", name])


def _resolve_tasks(engine: Engine) -> list[str]:
    """The posting identity key of every resolve task, sorted."""
    with Session(engine) as session:
        payloads = session.scalars(select(Task.payload).where(Task.kind == "resolve")).all()
        keys = [
            session.get_one(Posting, uuid.UUID(p["posting_id"])).identity_key for p in payloads
        ]
    return sorted(keys)


def test_extraction_queues_a_resolve_task_for_each_posting_with_new_values(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    _stored_fetch(engine, target_id, _page(2911234, 2911240))

    _extract(engine, queue_source_id)

    assert _resolve_tasks(engine) == ["2911234", "2911240"]


def test_a_posting_with_an_open_resolve_task_does_not_get_another(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    _stored_fetch(engine, target_id, _page(2911234))
    _extract(engine, queue_source_id)
    _stored_fetch(engine, target_id, _page(2911234), FIRST_SEEN + timedelta(days=1))

    _extract(engine, queue_source_id)

    assert _resolve_tasks(engine) == ["2911234"]


def test_an_unchanged_fetch_queues_no_resolve_task(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    target_id = add_target(url=LISTING_URL)
    first = _stored_fetch(engine, target_id, _page(2911234), queue_extract=False)
    _stored_fetch(
        engine,
        target_id,
        _page(2911234),
        FIRST_SEEN + timedelta(days=1),
        same_as=_capture_of(engine, first),
    )

    _extract(engine, queue_source_id)

    assert _resolve_tasks(engine) == []
