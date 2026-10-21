"""Tests for the EURAXESS adapter (sources/euraxess.py)."""

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from aoi.pipeline.sources.euraxess import (
    BASE,
    ListingItem,
    RawOpportunity,
    _extract_job_id,
    _listing_url,
    _next_page_url,
    harvest,
    parse_detail_page,
    parse_listing_page,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
LISTING_HTML = (FIXTURE_DIR / "euraxess_listing.html").read_text()
LISTING_LAST_HTML = (FIXTURE_DIR / "euraxess_listing_last.html").read_text()
DETAIL_HTML = (FIXTURE_DIR / "euraxess_detail.html").read_text()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_fetch_result(body: bytes, status: int = 200):  # type: ignore[no-untyped-def]
    from aoi.pipeline.fetch import FetchResult

    return FetchResult(
        url="https://euraxess.ec.europa.eu/jobs/search?page=0",
        status=status,
        headers={"content-type": "text/html"},
        body=body,
        elapsed_ms=200,
        not_modified=False,
        resolved_ip="147.67.116.12",
        final_url="https://euraxess.ec.europa.eu/jobs/search?page=0",
        http_version="HTTP/1.1",
        request_headers={},
    )


def _fake_evidence() -> object:
    return type(
        "StoredEvidence",
        (),
        {
            "fetch_id": str(uuid.uuid4()),
            "content_digest": "b" * 64,
            "prefix": "evidence/test/",
        },
    )()


# ---------------------------------------------------------------------------
# Unit tests — _extract_job_id
# ---------------------------------------------------------------------------


def test_extract_job_id_from_path() -> None:
    assert _extract_job_id("/jobs/468285") == "468285"


def test_extract_job_id_from_full_url() -> None:
    assert _extract_job_id("https://euraxess.ec.europa.eu/jobs/512340") == "512340"


def test_extract_job_id_no_match() -> None:
    assert _extract_job_id("/other/path") == ""


# ---------------------------------------------------------------------------
# Unit tests — _listing_url
# ---------------------------------------------------------------------------


def test_listing_url_page_zero() -> None:
    url = _listing_url(0)
    assert "page=0" in url
    assert url.startswith(BASE)


def test_listing_url_page_five() -> None:
    url = _listing_url(5)
    assert "page=5" in url


# ---------------------------------------------------------------------------
# Unit tests — parse_listing_page
# ---------------------------------------------------------------------------


def test_parse_listing_page_yields_three_items() -> None:
    items = parse_listing_page(LISTING_HTML, "https://euraxess.ec.europa.eu/jobs/search?page=0")
    assert len(items) == 3
    assert all(isinstance(i, ListingItem) for i in items)


def test_parse_listing_page_job_ids() -> None:
    items = parse_listing_page(LISTING_HTML, "https://euraxess.ec.europa.eu/jobs/search?page=0")
    ids = {i.job_id for i in items}
    assert ids == {"468285", "512340", "499001"}


def test_parse_listing_page_title_hints() -> None:
    items = parse_listing_page(LISTING_HTML, "https://euraxess.ec.europa.eu/jobs/search?page=0")
    titles = {i.title_hint for i in items}
    assert "Postdoctoral Researcher in Quantum Computing" in titles
    assert "PhD Position in Machine Learning for Climate Science" in titles


def test_parse_listing_page_detail_urls_canonical() -> None:
    items = parse_listing_page(LISTING_HTML, "https://euraxess.ec.europa.eu/jobs/search?page=0")
    for item in items:
        assert item.detail_url.startswith("https://euraxess.ec.europa.eu/jobs/")
        # Canonical: no trailing slash, no tracking params
        assert not item.detail_url.endswith("/")


def test_parse_listing_page_listing_url_stored() -> None:
    items = parse_listing_page(LISTING_HTML, "https://euraxess.ec.europa.eu/jobs/search?page=0")
    for item in items:
        assert "euraxess.ec.europa.eu" in item.listing_url


def test_parse_listing_page_last_page_single_item() -> None:
    items = parse_listing_page(
        LISTING_LAST_HTML, "https://euraxess.ec.europa.eu/jobs/search?page=1"
    )
    assert len(items) == 1
    assert items[0].job_id == "600100"


# ---------------------------------------------------------------------------
# Unit tests — _next_page_url
# ---------------------------------------------------------------------------


def test_next_page_url_returns_next_link() -> None:
    url = _next_page_url(LISTING_HTML)
    assert url is not None
    assert "page=1" in url


def test_next_page_url_last_page_returns_none() -> None:
    url = _next_page_url(LISTING_LAST_HTML)
    assert url is None


# ---------------------------------------------------------------------------
# Unit tests — parse_detail_page
# ---------------------------------------------------------------------------


def test_parse_detail_page_title() -> None:
    opp = parse_detail_page(
        DETAIL_HTML,
        detail_url="https://euraxess.ec.europa.eu/jobs/468285",
        listing_url="https://euraxess.ec.europa.eu/jobs/search?page=0",
        job_id="468285",
        fetch_id=uuid.uuid4(),
        content_digest="c" * 64,
    )
    assert opp.title == "Postdoctoral Researcher in Quantum Computing"


def test_parse_detail_page_fields() -> None:
    opp = parse_detail_page(
        DETAIL_HTML,
        detail_url="https://euraxess.ec.europa.eu/jobs/468285",
        listing_url="https://euraxess.ec.europa.eu/jobs/search?page=0",
        job_id="468285",
        fetch_id=uuid.uuid4(),
        content_digest="c" * 64,
    )
    assert opp.fields["Organisation/Company"] == "ETH Zurich"
    assert opp.fields["Country"] == "Switzerland"
    assert opp.fields["Research Field"] == "Physics"
    assert opp.fields["Job Status"] == "Open"


def test_parse_detail_page_deadline() -> None:
    opp = parse_detail_page(
        DETAIL_HTML,
        detail_url="https://euraxess.ec.europa.eu/jobs/468285",
        listing_url="https://euraxess.ec.europa.eu/jobs/search?page=0",
        job_id="468285",
        fetch_id=uuid.uuid4(),
        content_digest="c" * 64,
    )
    assert opp.deadline_iso == "2026-10-26T22:59:59+00:00"


def test_parse_detail_page_application_url() -> None:
    opp = parse_detail_page(
        DETAIL_HTML,
        detail_url="https://euraxess.ec.europa.eu/jobs/468285",
        listing_url="https://euraxess.ec.europa.eu/jobs/search?page=0",
        job_id="468285",
        fetch_id=uuid.uuid4(),
        content_digest="c" * 64,
    )
    assert opp.application_url == "https://ethz.ch/en/jobs/postdoc-12345.html"


def test_parse_detail_page_scrubs_personal_email() -> None:
    opp = parse_detail_page(
        DETAIL_HTML,
        detail_url="https://euraxess.ec.europa.eu/jobs/468285",
        listing_url="https://euraxess.ec.europa.eu/jobs/search?page=0",
        job_id="468285",
        fetch_id=uuid.uuid4(),
        content_digest="c" * 64,
    )
    # prof.mueller@ethz.ch is personal — should be scrubbed
    assert "prof.mueller@ethz.ch" not in opp.description
    assert "[contact removed]" in opp.description
    # jobs@ethz.ch is a role account — should be kept
    assert "jobs@ethz.ch" in opp.description


def test_parse_detail_page_document_text_scrubbed() -> None:
    opp = parse_detail_page(
        DETAIL_HTML,
        detail_url="https://euraxess.ec.europa.eu/jobs/468285",
        listing_url="https://euraxess.ec.europa.eu/jobs/search?page=0",
        job_id="468285",
        fetch_id=uuid.uuid4(),
        content_digest="c" * 64,
    )
    # document_text is the full page text, also scrubbed
    assert "prof.mueller@ethz.ch" not in opp.document_text
    assert "jobs@ethz.ch" in opp.document_text


def test_parse_detail_page_field_positions_populated() -> None:
    opp = parse_detail_page(
        DETAIL_HTML,
        detail_url="https://euraxess.ec.europa.eu/jobs/468285",
        listing_url="https://euraxess.ec.europa.eu/jobs/search?page=0",
        job_id="468285",
        fetch_id=uuid.uuid4(),
        content_digest="c" * 64,
    )
    # At least some fields should have positions in document_text
    assert len(opp.field_positions) > 0
    # Verify the positions actually point to the right text
    for label, (start, end) in opp.field_positions.items():
        assert opp.document_text[start:end] == opp.fields[label]


def test_parse_detail_page_job_id_stored() -> None:
    opp = parse_detail_page(
        DETAIL_HTML,
        detail_url="https://euraxess.ec.europa.eu/jobs/468285",
        listing_url="https://euraxess.ec.europa.eu/jobs/search?page=0",
        job_id="468285",
        fetch_id=uuid.uuid4(),
        content_digest="c" * 64,
    )
    assert opp.job_id == "468285"


# ---------------------------------------------------------------------------
# Unit tests — harvest (mocked network)
# ---------------------------------------------------------------------------


def test_harvest_yields_opportunities() -> None:
    evidence = _fake_evidence()

    call_count = 0

    def fake_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if "search" in url:
            return _make_fake_fetch_result(LISTING_HTML.encode())
        return _make_fake_fetch_result(DETAIL_HTML.encode())

    with (
        patch("aoi.pipeline.sources.euraxess.fetch", side_effect=fake_fetch),
        patch("aoi.pipeline.sources.euraxess.store_fetch", return_value=evidence),
    ):
        opps = list(harvest(uuid.uuid4(), max_pages=1))

    # 3 items on the listing page, each gets a detail fetch
    assert len(opps) == 3
    assert all(isinstance(o, RawOpportunity) for o in opps)


def test_harvest_early_stop_when_all_known() -> None:
    """When all items on a page are known and consecutive count hits threshold, stop."""
    evidence = _fake_evidence()

    fetch_call_count = 0

    def fake_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal fetch_call_count
        fetch_call_count += 1
        if "search" in url:
            return _make_fake_fetch_result(LISTING_HTML.encode())
        return _make_fake_fetch_result(DETAIL_HTML.encode())

    # All 3 IDs are known — with early_stop_consecutive=3, stops after 3 consecutive
    known = frozenset({"468285", "512340", "499001"})

    with (
        patch("aoi.pipeline.sources.euraxess.fetch", side_effect=fake_fetch),
        patch("aoi.pipeline.sources.euraxess.store_fetch", return_value=evidence),
    ):
        opps = list(harvest(uuid.uuid4(), max_pages=10, known_ids=known, early_stop_consecutive=3))

    # All 3 known → 3 consecutive → early stop, no detail fetches
    assert len(opps) == 0
    # Only 1 listing page fetched before early stop
    assert fetch_call_count == 1


def test_harvest_no_early_stop_below_consecutive_threshold() -> None:
    """When known count is below threshold, harvest continues."""
    evidence = _fake_evidence()

    fetch_call_count = 0

    def fake_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal fetch_call_count
        fetch_call_count += 1
        if "search" in url:
            # Use last page HTML (1 item) to stop pagination after 1 page
            return _make_fake_fetch_result(LISTING_LAST_HTML.encode())
        return _make_fake_fetch_result(DETAIL_HTML.encode())

    # 1 item on the last page (job_id 600100) — it is known, but 1 < 10 threshold
    known = frozenset({"600100"})

    with (
        patch("aoi.pipeline.sources.euraxess.fetch", side_effect=fake_fetch),
        patch("aoi.pipeline.sources.euraxess.store_fetch", return_value=evidence),
    ):
        opps = list(harvest(uuid.uuid4(), max_pages=10, known_ids=known))

    # Known item skipped (no detail fetch), but no early-stop (1 < 10)
    assert len(opps) == 0
    # 1 listing fetch, no detail fetches
    assert fetch_call_count == 1


def test_harvest_skips_known_detail_pages() -> None:
    evidence = _fake_evidence()

    detail_urls_fetched: list[str] = []

    def fake_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        if "search" not in url:
            detail_urls_fetched.append(url)
        if "search" in url:
            return _make_fake_fetch_result(LISTING_HTML.encode())
        return _make_fake_fetch_result(DETAIL_HTML.encode())

    # 1 of 3 is known — below consecutive threshold, so page continues
    known = frozenset({"468285"})

    with (
        patch("aoi.pipeline.sources.euraxess.fetch", side_effect=fake_fetch),
        patch("aoi.pipeline.sources.euraxess.store_fetch", return_value=evidence),
    ):
        opps = list(harvest(uuid.uuid4(), max_pages=1, known_ids=known))

    # Only 2 detail pages fetched (468285 skipped)
    assert len(opps) == 2
    assert all("468285" not in url for url in detail_urls_fetched)


def test_harvest_stops_on_empty_listing() -> None:
    empty_html = b"<html><body><main><div class='view-content'></div></main></body></html>"
    evidence = _fake_evidence()
    fetch_call_count = 0

    def fake_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal fetch_call_count
        fetch_call_count += 1
        return _make_fake_fetch_result(empty_html)

    with (
        patch("aoi.pipeline.sources.euraxess.fetch", side_effect=fake_fetch),
        patch("aoi.pipeline.sources.euraxess.store_fetch", return_value=evidence),
    ):
        opps = list(harvest(uuid.uuid4(), max_pages=10))

    assert len(opps) == 0
    assert fetch_call_count == 1


def test_harvest_stops_at_max_pages() -> None:
    evidence = _fake_evidence()
    listing_fetch_count = 0

    def fake_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal listing_fetch_count
        if "search" in url:
            listing_fetch_count += 1
            # Always return a page with a "next" link
            return _make_fake_fetch_result(LISTING_HTML.encode())
        return _make_fake_fetch_result(DETAIL_HTML.encode())

    with (
        patch("aoi.pipeline.sources.euraxess.fetch", side_effect=fake_fetch),
        patch("aoi.pipeline.sources.euraxess.store_fetch", return_value=evidence),
    ):
        list(harvest(uuid.uuid4(), max_pages=2))

    # Should fetch exactly 2 listing pages
    assert listing_fetch_count == 2


def test_harvest_stops_on_non_200_status() -> None:
    evidence = _fake_evidence()

    def fake_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        return _make_fake_fetch_result(b"Server Error", status=500)

    with (
        patch("aoi.pipeline.sources.euraxess.fetch", side_effect=fake_fetch),
        patch("aoi.pipeline.sources.euraxess.store_fetch", return_value=evidence),
    ):
        opps = list(harvest(uuid.uuid4(), max_pages=5))

    assert len(opps) == 0


def test_harvest_fetch_id_is_uuid() -> None:
    evidence = _fake_evidence()

    def fake_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        if "search" in url:
            return _make_fake_fetch_result(LISTING_HTML.encode())
        return _make_fake_fetch_result(DETAIL_HTML.encode())

    with (
        patch("aoi.pipeline.sources.euraxess.fetch", side_effect=fake_fetch),
        patch("aoi.pipeline.sources.euraxess.store_fetch", return_value=evidence),
    ):
        opps = list(harvest(uuid.uuid4(), max_pages=1))

    for opp in opps:
        assert isinstance(opp.fetch_id, uuid.UUID)


def test_harvest_continues_on_detail_fetch_error() -> None:
    """If fetching a detail page raises, harvest skips it and continues."""
    evidence = _fake_evidence()
    call_count = 0

    def fake_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if "search" in url:
            return _make_fake_fetch_result(LISTING_HTML.encode())
        # First detail page raises, others succeed
        if call_count == 2:
            raise ConnectionError("network timeout")
        return _make_fake_fetch_result(DETAIL_HTML.encode())

    with (
        patch("aoi.pipeline.sources.euraxess.fetch", side_effect=fake_fetch),
        patch("aoi.pipeline.sources.euraxess.store_fetch", return_value=evidence),
    ):
        opps = list(harvest(uuid.uuid4(), max_pages=1))

    # 3 items on listing, 1 detail fetch fails → 2 opportunities
    assert len(opps) == 2


def test_harvest_skips_non_200_detail_page() -> None:
    """If a detail page returns non-200, harvest skips it."""
    evidence = _fake_evidence()
    detail_call_count = 0

    def fake_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal detail_call_count
        if "search" in url:
            return _make_fake_fetch_result(LISTING_HTML.encode())
        detail_call_count += 1
        # First detail page returns 403
        if detail_call_count == 1:
            return _make_fake_fetch_result(b"Forbidden", status=403)
        return _make_fake_fetch_result(DETAIL_HTML.encode())

    with (
        patch("aoi.pipeline.sources.euraxess.fetch", side_effect=fake_fetch),
        patch("aoi.pipeline.sources.euraxess.store_fetch", return_value=evidence),
    ):
        opps = list(harvest(uuid.uuid4(), max_pages=1))

    # 3 items on listing, 1 returns 403 → 2 opportunities
    assert len(opps) == 2


def test_harvest_appends_query_param_to_detail_fetch_url() -> None:
    """Detail pages are fetched with ?x=1 to bypass 307 redirect, but
    RawOpportunity.detail_url stores the canonical URL without it."""
    evidence = _fake_evidence()

    detail_fetch_urls: list[str] = []

    def fake_fetch(url: str, **kwargs):  # type: ignore[no-untyped-def]
        if "search" not in url:
            detail_fetch_urls.append(url)
        if "search" in url:
            return _make_fake_fetch_result(LISTING_LAST_HTML.encode())
        return _make_fake_fetch_result(DETAIL_HTML.encode())

    with (
        patch("aoi.pipeline.sources.euraxess.fetch", side_effect=fake_fetch),
        patch("aoi.pipeline.sources.euraxess.store_fetch", return_value=evidence),
    ):
        opps = list(harvest(uuid.uuid4(), max_pages=1))

    assert len(opps) == 1
    # The fetch URL should contain ?x=1
    assert len(detail_fetch_urls) == 1
    assert "?x=1" in detail_fetch_urls[0]
    # But the stored detail_url should NOT have ?x=1
    assert "?x=1" not in opps[0].detail_url
    assert opps[0].detail_url == "https://euraxess.ec.europa.eu/jobs/600100"


@pytest.mark.live
def test_euraxess_live_listing() -> None:
    """Live test against the real EURAXESS site. Run manually only."""
    source_id = uuid.uuid4()
    opps = list(harvest(source_id, max_pages=1))
    assert len(opps) > 0
    assert all(isinstance(o, RawOpportunity) for o in opps)
