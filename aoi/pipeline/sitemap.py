"""Sitemap-driven URL discovery for AOI.

Fetches and parses XML sitemaps (sitemaps.org protocol), returning a flat list
of SitemapEntry objects.  Handles both formats:

- ``<urlset>``       — flat list of ``<url>/<loc>`` entries
- ``<sitemapindex>`` — list of child sitemaps; fetches each recursively

Usage::

    entries = fetch_sitemap("https://example.com/sitemap.xml", source_id)
    for entry in entries:
        if _should_fetch(entry, session):
            detail = fetch(entry.url, ...)
            opportunity = parse_detail_page(detail.body, ...)
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import structlog

from aoi.pipeline.fetch import fetch
from aoi.storage.evidence import store_fetch

log = structlog.get_logger()

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class SitemapEntry:
    """One entry from a parsed sitemap."""

    url: str
    lastmod: datetime | None  # None when <lastmod> is absent
    changefreq: str | None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_lastmod(text: str | None) -> datetime | None:
    """Parse a sitemap <lastmod> value to a UTC-aware datetime, or None."""
    if not text:
        return None
    s = text.strip()
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s).replace(tzinfo=UTC)
    except ValueError:
        log.debug("sitemap.parse.bad_lastmod", raw=s)
        return None


def _parse_urlset(root: ET.Element) -> list[SitemapEntry]:
    """Parse a <urlset> element into a list of SitemapEntry."""
    entries: list[SitemapEntry] = []
    for url_el in root.findall(f"{{{NS}}}url"):
        loc_el = url_el.find(f"{{{NS}}}loc")
        if loc_el is None or not (loc_el.text or "").strip():
            continue
        lastmod_el = url_el.find(f"{{{NS}}}lastmod")
        changefreq_el = url_el.find(f"{{{NS}}}changefreq")
        entries.append(
            SitemapEntry(
                url=(loc_el.text or "").strip(),
                lastmod=_parse_lastmod(lastmod_el.text if lastmod_el is not None else None),
                changefreq=(changefreq_el.text or "").strip() or None
                if changefreq_el is not None
                else None,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_sitemap(
    root_url: str,
    source_id: uuid.UUID,
    *,
    per_second: float = 1.0,
    _depth: int = 0,
) -> list[SitemapEntry]:
    """Fetch and parse a sitemap, returning a flat list of SitemapEntry.

    Handles both ``<urlset>`` (flat job-URL list) and ``<sitemapindex>``
    (nested child sitemaps).  For a sitemapindex, each child sitemap is
    fetched and parsed; their entries are concatenated into a single list.

    Recursion depth is limited to 1 (sitemapindex → urlset) — double-nested
    indexes are not a real-world concern and would indicate a malformed sitemap.

    Every HTTP response is stored in the evidence store.

    Args:
        root_url: URL of the sitemap to fetch.
        source_id: UUID of the ``Source`` row (for evidence storage).
        per_second: Politeness rate limit.
        _depth: Internal recursion depth guard; do not pass externally.

    Returns:
        Flat list of :class:`SitemapEntry`.  Empty list on fetch error or
        parse failure.
    """
    result = fetch(root_url, per_second=per_second, accept="application/xml")

    store_fetch(
        source_id=str(source_id),
        fetch_id=str(uuid.uuid4()),
        url=root_url,
        request_headers={"Accept": "application/xml"},
        status=result.status,
        response_headers=result.headers,
        body=result.body,
        fetched_at=datetime.now(UTC),
    )

    if result.status != 200:
        log.warning("sitemap.fetch.bad_status", url=root_url, status=result.status)
        return []

    try:
        root = ET.fromstring(result.body)
    except ET.ParseError:
        log.warning("sitemap.fetch.parse_error", url=root_url)
        return []

    tag = root.tag

    if tag == f"{{{NS}}}urlset":
        entries = _parse_urlset(root)
        log.info("sitemap.parsed.urlset", url=root_url, count=len(entries))
        return entries

    if tag == f"{{{NS}}}sitemapindex" and _depth == 0:
        all_entries: list[SitemapEntry] = []
        for sitemap_el in root.findall(f"{{{NS}}}sitemap"):
            loc_el = sitemap_el.find(f"{{{NS}}}loc")
            if loc_el is None or not (loc_el.text or "").strip():
                continue
            child_url = (loc_el.text or "").strip()
            log.debug("sitemap.index.child", child_url=child_url)
            child_entries = fetch_sitemap(
                child_url, source_id, per_second=per_second, _depth=_depth + 1
            )
            all_entries.extend(child_entries)
        log.info("sitemap.parsed.index", url=root_url, total=len(all_entries))
        return all_entries

    log.warning("sitemap.fetch.unknown_root", url=root_url, tag=tag)
    return []
