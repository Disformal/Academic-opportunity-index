"""The INSPIRE jobs adapter (docs/sources/inspire.md, ADR 0031) over real stored responses.

Fixtures (tests/fixtures/inspire/): listing_page.json is a stored listing page (25 jobs: 7 without
`urls`, 5 with several institutions, open and closed); single_job_3215677.json is one job fetched
through the search endpoint. Both hold only the permitted fields and no email addresses.

Every value carries a JSON pointer that resolves, in the document, to the value it came from. A
job without `urls` gets INSPIRE's own job page as its application URL, derived from its control
number with lower confidence. The adapter never produces a record-endpoint URL (/api/jobs/<id>
ignores `fields`), and a response with a field we did not request is refused.
"""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from aoi.adapters.inspire import (
    FIELDS,
    OPEN_LISTING,
    ForbiddenContent,
    InspireAdapter,
    archive_url,
    job_url,
)
from aoi.adapters.protocol import Adapter, Document, ParsedPosting
from aoi.adapters.registry import ADAPTERS
from aoi.db.schema.enums import AssertionMethod, OpportunityKind, PostingIdentityKind, TargetRole

FIXTURES = Path(__file__).parent / "fixtures" / "inspire"
RECORD_ENDPOINT = re.compile(r"/api/jobs/\d")


def _doc(name: str, body: bytes | None = None) -> Document:
    return Document(
        url="https://inspirehep.net/api/jobs?q=&size=25&page=8",
        headers={"content-type": "application/json"},
        body=body if body is not None else (FIXTURES / name).read_bytes(),
        fetched_at=datetime(2026, 10, 20, 11, 53, tzinfo=UTC),
    )


def _resolve(document: Any, pointer: str) -> Any:
    for part in pointer.lstrip("/").split("/"):
        document = document[int(part)] if isinstance(document, list) else document[part]
    return document


def _parsed(name: str = "listing_page.json") -> list[ParsedPosting]:
    return InspireAdapter().parse_detail(_doc(name))


def _fields(posting: ParsedPosting) -> dict[str, Any]:
    return {a.field: a for a in posting.assertions}


def test_the_adapter_meets_the_protocol_and_is_registered() -> None:
    assert isinstance(InspireAdapter(), Adapter)
    assert isinstance(ADAPTERS["inspire"], InspireAdapter)


def test_discovery_starts_from_the_open_jobs_listing() -> None:
    (spec,) = InspireAdapter().discover(source=None)  # type: ignore[arg-type]

    assert spec.url == OPEN_LISTING
    assert spec.role == TargetRole.LISTING
    assert "q=status%3Aopen" in OPEN_LISTING and "size=250" in OPEN_LISTING


@pytest.mark.parametrize("url", [OPEN_LISTING, archive_url(2015), job_url("3215677")])
def test_every_url_asks_for_the_permitted_fields_and_never_the_record_endpoint(url: str) -> None:
    assert f"fields={','.join(FIELDS)}" in url
    assert not RECORD_ENDPOINT.search(url)


def test_an_archive_slice_is_one_deadline_year() -> None:
    assert "q=deadline_date%3A%5B2015-01-01%20TO%202015-12-31%5D" in archive_url(2015)


def test_a_listing_shows_its_jobs_and_links_to_its_next_page() -> None:
    data = json.loads((FIXTURES / "listing_page.json").read_bytes())

    page = InspireAdapter().parse_listing(_doc("listing_page.json"))

    assert [r.identity_key for r in page.refs] == [
        str(h["metadata"]["control_number"]) for h in data["hits"]["hits"]
    ]
    assert {r.identity_kind for r in page.refs} == {PostingIdentityKind.NATIVE_REF}
    assert {r.kind for r in page.refs} == {OpportunityKind.JOB}
    assert [t.url for t in page.targets] == [data["links"]["next"]]
    assert {t.role for t in page.targets} == {TargetRole.LISTING}


def test_a_jobs_detail_url_is_its_search_endpoint_query() -> None:
    page = InspireAdapter().parse_listing(_doc("listing_page.json"))

    for ref in page.refs:
        assert ref.detail_url == job_url(ref.identity_key)
        assert not RECORD_ENDPOINT.search(ref.detail_url or "")


def test_a_last_page_links_to_nothing() -> None:
    data = json.loads((FIXTURES / "listing_page.json").read_bytes())
    del data["links"]["next"]

    page = InspireAdapter().parse_listing(_doc("", json.dumps(data).encode()))

    assert page.targets == ()


def test_every_job_on_a_page_is_parsed_with_its_identity() -> None:
    parsed = _parsed()

    assert len(parsed) == 25
    assert len({p.ref.identity_key for p in parsed}) == 25


def test_every_value_points_at_where_it_came_from() -> None:
    data = json.loads((FIXTURES / "listing_page.json").read_bytes())

    for posting in _parsed():
        for a in posting.assertions:
            assert a.method == AssertionMethod.OFFICIAL_API
            assert a.pointer is not None
            source = _resolve(data, a.pointer)
            if a.field == "application_url" and a.confidence < 1:
                assert a.value == f"https://inspirehep.net/jobs/{source}"
            elif a.field == "organizations":
                assert a.value == [i["value"] for i in source]
            elif a.field == "inspire_institution_ids":
                assert a.value == [
                    i["record"]["$ref"].rsplit("/", 1)[1] if "record" in i else None
                    for i in source
                ]
            elif a.field == "experiments":
                assert a.value == [e["name"] for e in source]
            else:
                assert a.value == source


def test_every_job_has_the_fields_an_active_posting_needs() -> None:
    for posting in _parsed():
        assert {"title", "application_url", "status", "deadline", "posted_at"} <= set(
            _fields(posting)
        )


def test_a_job_without_urls_applies_through_its_inspire_page_with_lower_confidence() -> None:
    data = json.loads((FIXTURES / "listing_page.json").read_bytes())
    without = {
        str(h["metadata"]["control_number"])
        for h in data["hits"]["hits"]
        if not h["metadata"].get("urls")
    }

    derived = {
        p.ref.identity_key: _fields(p)["application_url"]
        for p in _parsed()
        if p.ref.identity_key in without
    }

    assert len(derived) == 7
    for key, url in derived.items():
        assert url.value == f"https://inspirehep.net/jobs/{key}"
        assert url.confidence == Decimal("0.5")
        assert url.raw == key


def test_a_job_keeps_every_institution() -> None:
    by_key = {p.ref.identity_key: _fields(p) for p in _parsed()}

    assert by_key["3204632"]["organizations"].value == ["INFN, Turin", "U. Turin (main)"]
    assert by_key["3204632"]["inspire_institution_ids"].value == ["902889", "922847"]


def test_an_institution_without_an_inspire_record_keeps_its_place_in_the_id_list() -> None:
    data = json.loads((FIXTURES / "listing_page.json").read_bytes())
    index, job = next(
        (i, h)
        for i, h in enumerate(data["hits"]["hits"])
        if any("record" not in inst for inst in h["metadata"]["institutions"])
    )
    institutions = job["metadata"]["institutions"]
    expected = [
        inst["record"]["$ref"].rsplit("/", 1)[1] if "record" in inst else None
        for inst in institutions
    ]

    fields = _fields(_parsed()[index])

    assert fields["organizations"].value == [inst["value"] for inst in institutions]
    if any(expected):
        assert fields["inspire_institution_ids"].value == expected
    else:
        assert "inspire_institution_ids" not in fields


def test_the_posting_date_is_the_records_creation_unless_it_has_a_legacy_date() -> None:
    data = json.loads((FIXTURES / "listing_page.json").read_bytes())
    data["hits"]["hits"][1]["metadata"]["legacy_creation_date"] = "2009-03-02"

    parsed = InspireAdapter().parse_detail(_doc("", json.dumps(data).encode()))

    assert _fields(parsed[0])["posted_at"].value == data["hits"]["hits"][0]["created"]
    assert _fields(parsed[0])["posted_at"].pointer == "/hits/hits/0/created"
    assert _fields(parsed[1])["posted_at"].value == "2009-03-02"


def test_a_single_job_from_the_search_endpoint_is_parsed() -> None:
    (posting,) = _parsed("single_job_3215677.json")

    assert posting.ref.identity_key == "3215677"
    assert _fields(posting)["status"].value in ("open", "closed")


def test_a_response_with_a_field_we_did_not_request_is_refused() -> None:
    data = json.loads((FIXTURES / "single_job_3215677.json").read_bytes())
    data["hits"]["hits"][0]["metadata"]["contact_details"] = [{"name": "x"}]
    doc = _doc("", json.dumps(data).encode())

    with pytest.raises(ForbiddenContent):
        InspireAdapter().parse_detail(doc)
    with pytest.raises(ForbiddenContent):
        InspireAdapter().parse_listing(doc)


@pytest.mark.integration
def test_a_stored_page_becomes_resolved_postings_with_their_lifecycle(
    engine: Any, queue_source_id: Any, add_target: Any
) -> None:
    """Through the real extract, resolve and lifecycle steps and the database's own checks."""
    import uuid

    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from aoi.db.schema.core import Assertion, Posting, Source
    from aoi.db.schema.enums import ClosureReason, FetchOutcome, PostingState
    from aoi.db.schema.evidence import Fetch
    from aoi.db.schema.ops import CrawlTarget, Task
    from aoi.storage.warc import Exchange, store_exchange
    from aoi.tasks.handlers import handlers_for
    from aoi.tasks.handlers.lifecycle import evaluate
    from aoi.tasks.worker import run_once

    data = json.loads((FIXTURES / "listing_page.json").read_bytes())
    target_id = add_target(url=OPEN_LISTING)
    fetched = datetime(2026, 10, 20, 11, 53, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        name = session.get_one(Source, queue_source_id).name
        fetch_id = uuid.uuid4()
        stored = store_exchange(
            fetch_id,
            Exchange(
                url=OPEN_LISTING,
                request_headers={"host": "inspirehep.net"},
                http_version="HTTP/1.1",
                status=200,
                response_headers={"content-type": "application/json"},
                body=(FIXTURES / "listing_page.json").read_bytes(),
                fetched_at=fetched,
            ),
        )
        session.add(
            Fetch(
                id=fetch_id,
                crawl_target_id=target_id,
                url=OPEN_LISTING,
                final_url=OPEN_LISTING,
                requested_at=fetched,
                outcome=FetchOutcome.HIT,
                http_status=200,
                warc_record_id=stored.record_id,
                warc_key=stored.key,
            )
        )
        session.add(
            Task(kind="extract", source_id=queue_source_id, payload={"fetch_id": str(fetch_id)})
        )
    handlers = handlers_for(["extract", "resolve"], {name: InspireAdapter()})
    while run_once(engine, handlers, worker="w1"):
        pass
    with Session(engine) as session, session.begin():
        evaluate(session, queue_source_id, now=fetched)

    with Session(engine) as session:
        states = dict(
            session.execute(
                select(Posting.identity_key, Posting.state).where(
                    Posting.source_id == queue_source_id
                )
            ).all()
        )
        assertions = session.scalar(select(func.count()).select_from(Assertion))
        page_two = session.scalars(
            select(CrawlTarget.url).where(CrawlTarget.id != target_id)
        ).all()
        reasons = set(
            session.scalars(
                select(Posting.closure_reason).where(Posting.state == PostingState.CLOSED)
            )
        )
    # Stated status decides: closed → closed(stated_closed); open stays active even past its
    # deadline, because the lifecycle only closes on a deadline when the job is not stated open.
    assert states == {
        str(h["metadata"]["control_number"]): (
            PostingState.CLOSED if h["metadata"]["status"] == "closed" else PostingState.ACTIVE
        )
        for h in data["hits"]["hits"]
    }
    assert reasons == {ClosureReason.STATED_CLOSED}
    assert assertions is not None and assertions > 25 * 5
    assert len(page_two) == 1
