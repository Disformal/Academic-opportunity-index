"""The adapter protocol (plan §4.4, ADR 0031): adapters are pure functions of a stored Document.

A Document is built from WARC evidence, so the same parse can be re-run later by replay.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from fake_adapter import FakeApiAdapter

from aoi.adapters.protocol import Adapter, Document, PostingRef
from aoi.db.schema.enums import PostingIdentityKind, TargetRole
from aoi.storage.warc import EarlierCapture, Exchange, read_capture, store_exchange

FETCHED_AT = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
PAGE = {
    "hits": [
        {"id": 2911234, "title": "Postdoc", "url": "https://inspirehep.net/jobs/2911234"},
        {
            "id": 2911240,
            "title": "Θέση μεταδιδάκτορα στη Θεωρητική Φυσική",
            "url": "https://inspirehep.net/jobs/2911240",
        },
    ],
    "next": "https://inspirehep.net/api/jobs?page=2",
}


def _document(page: dict[str, object] = PAGE) -> Document:
    return Document(
        url="https://inspirehep.net/api/jobs?page=1",
        headers={"Content-Type": "application/json"},
        body=json.dumps(page).encode(),
        fetched_at=FETCHED_AT,
    )


def _exchange(body: bytes) -> Exchange:
    return Exchange(
        url="https://inspirehep.net/api/jobs?page=1",
        request_headers={"Host": "inspirehep.net"},
        http_version="HTTP/2",
        status=200,
        response_headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        body=body,
        fetched_at=FETCHED_AT,
    )


@pytest.mark.integration
def test_a_document_from_stored_evidence_has_its_url_headers_body_and_time() -> None:
    body = json.dumps(PAGE).encode()
    capture = read_capture(store_exchange(uuid.uuid4(), _exchange(body)).key)

    doc = Document.from_capture(capture)

    assert doc.url == "https://inspirehep.net/api/jobs?page=1"
    assert doc.headers["Content-Type"] == "application/json"
    assert doc.headers["X-Archive-Orig-Content-Encoding"] == "gzip"
    assert doc.body == body
    assert doc.fetched_at == FETCHED_AT


@pytest.mark.integration
def test_a_revisit_capture_is_not_a_document() -> None:
    first = store_exchange(uuid.uuid4(), _exchange(b"{}"))
    revisit = store_exchange(
        uuid.uuid4(),
        _exchange(b"{}"),
        same_as=EarlierCapture(
            first.record_id, "https://inspirehep.net/api/jobs?page=1", FETCHED_AT
        ),
    )

    with pytest.raises(ValueError, match="revisit"):
        Document.from_capture(read_capture(revisit.key))


def test_the_fake_adapter_conforms_to_the_protocol() -> None:
    assert isinstance(FakeApiAdapter(), Adapter)


def test_a_listing_returns_the_postings_it_shows_and_the_next_page() -> None:
    page = FakeApiAdapter().parse_listing(_document())

    assert [r.identity_key for r in page.refs] == ["2911234", "2911240"]
    assert [(t.url, t.role) for t in page.targets] == [
        ("https://inspirehep.net/api/jobs?page=2", TargetRole.LISTING)
    ]


def test_the_last_listing_page_has_no_next_target() -> None:
    page = FakeApiAdapter().parse_listing(_document({**PAGE, "next": None}))

    assert page.targets == ()


def test_one_api_document_yields_many_postings_each_with_its_identity() -> None:
    parsed = FakeApiAdapter().parse_detail(_document())

    assert [p.ref for p in parsed] == [
        PostingRef(
            PostingIdentityKind.NATIVE_REF, "2911234", "https://inspirehep.net/jobs/2911234"
        ),
        PostingRef(
            PostingIdentityKind.NATIVE_REF, "2911240", "https://inspirehep.net/jobs/2911240"
        ),
    ]
    assert parsed[1].assertions[0].value == "Θέση μεταδιδάκτορα στη Θεωρητική Φυσική"
    assert parsed[1].assertions[0].pointer == "/hits/1/title"


def test_parsing_is_a_pure_function_of_the_document() -> None:
    adapter, doc = FakeApiAdapter(), _document()

    assert adapter.parse_detail(doc) == adapter.parse_detail(doc)
    assert adapter.parse_listing(doc) == adapter.parse_listing(doc)
