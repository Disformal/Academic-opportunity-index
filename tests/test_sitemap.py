"""Tests for the sitemap URL discovery module (pipeline/sitemap.py)."""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from aoi.pipeline.sitemap import SitemapEntry, fetch_sitemap

FIXTURE_URLSET = Path(__file__).parent / "fixtures" / "sitemap_urlset.xml"
FIXTURE_INDEX = Path(__file__).parent / "fixtures" / "sitemap_index.xml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fetch_result(body: bytes, status: int = 200):  # type: ignore[no-untyped-def]
    from aoi.pipeline.fetch import FetchResult

    return FetchResult(
        url="https://euraxess.ec.europa.eu/sitemap.xml",
        status=status,
        headers={"content-type": "application/xml"},
        body=body,
        elapsed_ms=60,
        not_modified=False,
        resolved_ip="1.2.3.4",
        final_url="https://euraxess.ec.europa.eu/sitemap.xml",
        http_version="HTTP/1.1",
        request_headers={},
    )


def _make_evidence() -> object:
    return type(
        "StoredEvidence",
        (),
        {
            "fetch_id": str(uuid.uuid4()),
            "content_digest": "f" * 64,
            "prefix": "evidence/test/",
        },
    )()


# ---------------------------------------------------------------------------
# Unit tests — no network, no DB
# ---------------------------------------------------------------------------


def test_fetch_sitemap_returns_entries_from_urlset() -> None:
    fake_result = _make_fetch_result(FIXTURE_URLSET.read_bytes())
    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.sitemap.fetch", return_value=fake_result),
        patch("aoi.pipeline.sitemap.store_fetch", return_value=fake_evidence),
    ):
        entries = fetch_sitemap("https://euraxess.ec.europa.eu/sitemap.xml", source_id)

    assert len(entries) == 3
    assert all(isinstance(e, SitemapEntry) for e in entries)


def test_fetch_sitemap_entry_urls_correct() -> None:
    fake_result = _make_fetch_result(FIXTURE_URLSET.read_bytes())
    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.sitemap.fetch", return_value=fake_result),
        patch("aoi.pipeline.sitemap.store_fetch", return_value=fake_evidence),
    ):
        entries = fetch_sitemap("https://euraxess.ec.europa.eu/sitemap.xml", source_id)

    urls = {e.url for e in entries}
    assert "https://euraxess.ec.europa.eu/jobs/468285" in urls
    assert "https://euraxess.ec.europa.eu/jobs/471001" in urls
    assert "https://euraxess.ec.europa.eu/jobs/472500" in urls


def test_fetch_sitemap_entry_lastmod_parsed_as_datetime() -> None:
    fake_result = _make_fetch_result(FIXTURE_URLSET.read_bytes())
    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.sitemap.fetch", return_value=fake_result),
        patch("aoi.pipeline.sitemap.store_fetch", return_value=fake_evidence),
    ):
        entries = fetch_sitemap("https://euraxess.ec.europa.eu/sitemap.xml", source_id)

    entry = next(e for e in entries if "468285" in e.url)
    assert isinstance(entry.lastmod, datetime)
    assert entry.lastmod == datetime(2026, 8, 20, tzinfo=UTC)


def test_fetch_sitemap_entry_without_lastmod_is_none() -> None:
    fake_result = _make_fetch_result(FIXTURE_URLSET.read_bytes())
    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.sitemap.fetch", return_value=fake_result),
        patch("aoi.pipeline.sitemap.store_fetch", return_value=fake_evidence),
    ):
        entries = fetch_sitemap("https://euraxess.ec.europa.eu/sitemap.xml", source_id)

    entry = next(e for e in entries if "472500" in e.url)
    assert entry.lastmod is None


def test_fetch_sitemap_entry_changefreq_populated() -> None:
    fake_result = _make_fetch_result(FIXTURE_URLSET.read_bytes())
    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.sitemap.fetch", return_value=fake_result),
        patch("aoi.pipeline.sitemap.store_fetch", return_value=fake_evidence),
    ):
        entries = fetch_sitemap("https://euraxess.ec.europa.eu/sitemap.xml", source_id)

    entry = next(e for e in entries if "468285" in e.url)
    assert entry.changefreq == "weekly"


def test_fetch_sitemap_follows_sitemapindex() -> None:
    """A sitemapindex triggers recursive fetching of each child sitemap."""
    child_xml = FIXTURE_URLSET.read_bytes()
    index_xml = FIXTURE_INDEX.read_bytes()

    def mock_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        body = index_xml if "sitemap.xml" in url and "jobs" not in url else child_xml
        from aoi.pipeline.fetch import FetchResult

        return FetchResult(
            url=url,
            status=200,
            headers={"content-type": "application/xml"},
            body=body,
            elapsed_ms=60,
            not_modified=False,
            resolved_ip="1.2.3.4",
            final_url=url,
            http_version="HTTP/1.1",
            request_headers={},
        )

    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.sitemap.fetch", side_effect=mock_fetch),
        patch("aoi.pipeline.sitemap.store_fetch", return_value=fake_evidence),
    ):
        entries = fetch_sitemap("https://euraxess.ec.europa.eu/sitemap.xml", source_id)

    # 2 child sitemaps × 3 entries each = 6 total
    assert len(entries) == 6


def test_fetch_sitemap_bad_status_returns_empty() -> None:
    fake_result = _make_fetch_result(b"<error/>", status=404)
    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.sitemap.fetch", return_value=fake_result),
        patch("aoi.pipeline.sitemap.store_fetch", return_value=fake_evidence),
    ):
        entries = fetch_sitemap("https://euraxess.ec.europa.eu/sitemap.xml", source_id)

    assert entries == []


def test_fetch_sitemap_invalid_xml_returns_empty() -> None:
    fake_result = _make_fetch_result(b"this is not xml", status=200)
    fake_evidence = _make_evidence()
    source_id = uuid.uuid4()

    with (
        patch("aoi.pipeline.sitemap.fetch", return_value=fake_result),
        patch("aoi.pipeline.sitemap.store_fetch", return_value=fake_evidence),
    ):
        entries = fetch_sitemap("https://euraxess.ec.europa.eu/sitemap.xml", source_id)

    assert entries == []


@pytest.mark.live
def test_euraxess_sitemap_live() -> None:
    """Live test against the real EURAXESS sitemap. Run manually only."""
    source_id = uuid.uuid4()
    entries = fetch_sitemap("https://euraxess.ec.europa.eu/sitemap.xml", source_id)
    assert len(entries) > 0
    assert all(isinstance(e, SitemapEntry) for e in entries)
