"""Tests for the OAI-PMH harvester (pipeline/oai_pmh.py)."""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest

from aoi.pipeline.oai_pmh import OAIRecord, harvest_oai, parse_record

FIXTURE_PAGE1 = Path(__file__).parent / "fixtures" / "oai_pmh_page1.xml"
FIXTURE_PAGE2 = Path(__file__).parent / "fixtures" / "oai_pmh_page2.xml"

NS_OAI = "http://www.openarchives.org/OAI/2.0/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fetch_result(body: bytes, status: int = 200):  # type: ignore[no-untyped-def]
    from aoi.pipeline.fetch import FetchResult

    return FetchResult(
        url="https://zenodo.org/oai2d",
        status=status,
        headers={"content-type": "application/xml"},
        body=body,
        elapsed_ms=80,
        not_modified=False,
        resolved_ip="1.2.3.4",
        final_url="https://zenodo.org/oai2d",
        http_version="HTTP/1.1",
        request_headers={},
    )


def _make_evidence(fetch_id: str | None = None) -> object:
    return type(
        "StoredEvidence",
        (),
        {
            "fetch_id": fetch_id or str(uuid.uuid4()),
            "content_digest": "b" * 64,
            "prefix": "evidence/test/",
        },
    )()


# ---------------------------------------------------------------------------
# parse_record unit tests (no network, no DB)
# ---------------------------------------------------------------------------


def _first_record(xml_bytes: bytes) -> ET.Element:
    """Return the first <record> element from an OAI-PMH response."""
    root = ET.fromstring(xml_bytes)
    return root.find(f".//{{{NS_OAI}}}record")  # type: ignore[return-value]


def test_parse_record_returns_oai_record() -> None:
    record_el = _first_record(FIXTURE_PAGE1.read_bytes())
    fetch_id = uuid.uuid4()
    record = parse_record(record_el, fetch_id, "c" * 64)
    assert isinstance(record, OAIRecord)


def test_parse_record_identifier() -> None:
    record_el = _first_record(FIXTURE_PAGE1.read_bytes())
    record = parse_record(record_el, uuid.uuid4(), "c" * 64)
    assert record is not None
    assert record.identifier == "oai:zenodo.org:12345"


def test_parse_record_datestamp_is_utc_aware_datetime() -> None:
    record_el = _first_record(FIXTURE_PAGE1.read_bytes())
    record = parse_record(record_el, uuid.uuid4(), "c" * 64)
    assert record is not None
    assert isinstance(record.datestamp, datetime)
    assert record.datestamp.tzinfo is not None


def test_parse_record_datestamp_value() -> None:
    record_el = _first_record(FIXTURE_PAGE1.read_bytes())
    record = parse_record(record_el, uuid.uuid4(), "c" * 64)
    assert record is not None
    assert record.datestamp.year == 2026
    assert record.datestamp.month == 8
    assert record.datestamp.day == 15


def test_parse_record_sets_populated() -> None:
    record_el = _first_record(FIXTURE_PAGE1.read_bytes())
    record = parse_record(record_el, uuid.uuid4(), "c" * 64)
    assert record is not None
    assert "user-hep" in record.sets


def test_parse_record_dc_title_in_metadata() -> None:
    record_el = _first_record(FIXTURE_PAGE1.read_bytes())
    record = parse_record(record_el, uuid.uuid4(), "c" * 64)
    assert record is not None
    assert "title" in record.metadata
    assert record.metadata["title"] == ["Postdoctoral Fellowship in High Energy Physics"]


def test_parse_record_dc_publisher_in_metadata() -> None:
    record_el = _first_record(FIXTURE_PAGE1.read_bytes())
    record = parse_record(record_el, uuid.uuid4(), "c" * 64)
    assert record is not None
    assert record.metadata.get("publisher") == ["CERN"]


def test_parse_record_multiple_identifiers_collected() -> None:
    """Both dc:identifier values should be collected as a list."""
    record_el = _first_record(FIXTURE_PAGE1.read_bytes())
    record = parse_record(record_el, uuid.uuid4(), "c" * 64)
    assert record is not None
    identifiers = record.metadata.get("identifier", [])
    assert len(identifiers) == 2
    assert "https://zenodo.org/record/12345" in identifiers
    assert "oai:zenodo.org:12345" in identifiers


def test_parse_record_deleted_returns_none() -> None:
    """A <record> with header status='deleted' should return None."""
    root = ET.fromstring(FIXTURE_PAGE2.read_bytes())
    records = root.findall(f".//{{{NS_OAI}}}record")
    # Second record in page2 has status="deleted"
    deleted = next(
        r
        for r in records
        if r.find(f"{{{NS_OAI}}}header") is not None
        and r.find(f"{{{NS_OAI}}}header").get("status") == "deleted"  # type: ignore[union-attr]
    )
    result = parse_record(deleted, uuid.uuid4(), "c" * 64)
    assert result is None


def test_parse_record_fetch_id_stored() -> None:
    record_el = _first_record(FIXTURE_PAGE1.read_bytes())
    fetch_id = uuid.uuid4()
    record = parse_record(record_el, fetch_id, "d" * 64)
    assert record is not None
    assert record.fetch_id == fetch_id


def test_parse_record_content_digest_stored() -> None:
    record_el = _first_record(FIXTURE_PAGE1.read_bytes())
    digest = "e" * 64
    record = parse_record(record_el, uuid.uuid4(), digest)
    assert record is not None
    assert record.content_digest == digest


# ---------------------------------------------------------------------------
# harvest_oai unit tests (mock fetch + store_fetch)
# ---------------------------------------------------------------------------


def test_harvest_oai_yields_records_from_single_page() -> None:
    """Single page with empty resumptionToken yields 2 records."""
    # Use page1 XML but replace the token so it looks like a terminal page
    xml = FIXTURE_PAGE1.read_bytes().replace(b"abc123token", b"")

    fake_result = _make_fetch_result(xml)
    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.oai_pmh.fetch", return_value=fake_result),
        patch("aoi.pipeline.oai_pmh.store_fetch", return_value=fake_evidence),
    ):
        records = list(harvest_oai("https://zenodo.org/oai2d", source_id=source_id))

    assert len(records) == 2
    assert all(isinstance(r, OAIRecord) for r in records)


def test_harvest_oai_record_identifiers_correct() -> None:
    xml = FIXTURE_PAGE1.read_bytes().replace(b"abc123token", b"")
    fake_result = _make_fetch_result(xml)
    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.oai_pmh.fetch", return_value=fake_result),
        patch("aoi.pipeline.oai_pmh.store_fetch", return_value=fake_evidence),
    ):
        records = list(harvest_oai("https://zenodo.org/oai2d", source_id=source_id))

    identifiers = {r.identifier for r in records}
    assert "oai:zenodo.org:12345" in identifiers
    assert "oai:zenodo.org:67890" in identifiers


def test_harvest_oai_follows_resumption_token() -> None:
    """Two pages: page1 has resumptionToken, page2 has empty token.
    Yields 3 records total (2 from page1 + 1 non-deleted from page2).
    """
    page1 = FIXTURE_PAGE1.read_bytes()
    page2 = FIXTURE_PAGE2.read_bytes()

    fetch_call_count = 0

    def mock_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal fetch_call_count
        fetch_call_count += 1
        body = page2 if "resumptionToken" in url else page1
        return _make_fetch_result(body)

    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.oai_pmh.fetch", side_effect=mock_fetch),
        patch("aoi.pipeline.oai_pmh.store_fetch", return_value=fake_evidence),
    ):
        records = list(harvest_oai("https://zenodo.org/oai2d", source_id=source_id))

    assert fetch_call_count == 2
    # 2 from page1 + 1 non-deleted from page2 (deleted record skipped)
    assert len(records) == 3


def test_harvest_oai_skips_deleted_records() -> None:
    """Records with header status='deleted' are not yielded."""
    page2 = FIXTURE_PAGE2.read_bytes()
    fake_result = _make_fetch_result(page2)
    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.oai_pmh.fetch", return_value=fake_result),
        patch("aoi.pipeline.oai_pmh.store_fetch", return_value=fake_evidence),
    ):
        records = list(harvest_oai("https://zenodo.org/oai2d", source_id=source_id))

    # page2 has 1 live record + 1 deleted; only 1 should be yielded
    assert len(records) == 1
    assert records[0].identifier == "oai:zenodo.org:99999"


def test_harvest_oai_from_date_included_in_url() -> None:
    """When from_date is supplied, the URL must contain a 'from' parameter."""
    xml = FIXTURE_PAGE1.read_bytes().replace(b"abc123token", b"")
    fetched_urls: list[str] = []

    def mock_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        fetched_urls.append(url)
        return _make_fetch_result(xml)

    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()
    from_date = datetime(2026, 9, 1, tzinfo=UTC)

    with (
        patch("aoi.pipeline.oai_pmh.fetch", side_effect=mock_fetch),
        patch("aoi.pipeline.oai_pmh.store_fetch", return_value=fake_evidence),
    ):
        list(harvest_oai("https://zenodo.org/oai2d", source_id=source_id, from_date=from_date))

    assert len(fetched_urls) == 1
    assert "from=2026-09-01" in fetched_urls[0]


def test_harvest_oai_set_spec_included_in_url() -> None:
    xml = FIXTURE_PAGE1.read_bytes().replace(b"abc123token", b"")
    fetched_urls: list[str] = []

    def mock_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        fetched_urls.append(url)
        return _make_fetch_result(xml)

    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.oai_pmh.fetch", side_effect=mock_fetch),
        patch("aoi.pipeline.oai_pmh.store_fetch", return_value=fake_evidence),
    ):
        list(
            harvest_oai(
                "https://zenodo.org/oai2d",
                source_id=source_id,
                set_spec="user-hep",
            )
        )

    assert "set=user-hep" in fetched_urls[0]


def test_harvest_oai_bad_status_stops_immediately() -> None:
    fake_result = _make_fetch_result(b"<error/>", status=503)
    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.oai_pmh.fetch", return_value=fake_result),
        patch("aoi.pipeline.oai_pmh.store_fetch", return_value=fake_evidence),
    ):
        records = list(harvest_oai("https://zenodo.org/oai2d", source_id=source_id))

    assert records == []


def test_harvest_oai_records_have_fetch_id_uuid() -> None:
    xml = FIXTURE_PAGE1.read_bytes().replace(b"abc123token", b"")
    fixed_fetch_id = str(uuid.uuid4())

    fake_result = _make_fetch_result(xml)
    fake_evidence = _make_evidence(fetch_id=fixed_fetch_id)
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.oai_pmh.fetch", return_value=fake_result),
        patch("aoi.pipeline.oai_pmh.store_fetch", return_value=fake_evidence),
    ):
        records = list(harvest_oai("https://zenodo.org/oai2d", source_id=source_id))

    for record in records:
        assert isinstance(record.fetch_id, uuid.UUID)
        assert record.fetch_id == uuid.UUID(fixed_fetch_id)


@pytest.mark.live
def test_harvest_oai_zenodo_live() -> None:
    """Live test against the real Zenodo OAI-PMH endpoint. Run manually only."""
    source_id = uuid.uuid4()
    records = list(
        harvest_oai(
            "https://zenodo.org/oai2d",
            source_id=source_id,
            set_spec="user-hep",
            metadata_prefix="oai_dc",
        )
    )
    assert len(records) > 0
    assert all(isinstance(r, OAIRecord) for r in records)
