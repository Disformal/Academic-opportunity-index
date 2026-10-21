"""EURAXESS HTML adapter — listing-detail model with early-stop."""

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin

import structlog
from selectolax.parser import HTMLParser
from sqlalchemy.orm import Session

from aoi.pipeline.canonicalize import canonicalize
from aoi.pipeline.fetch import fetch
from aoi.pipeline.privacy import scrub
from aoi.pipeline.structured_extract import extract_main_text
from aoi.storage.evidence import store_fetch

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE = "https://euraxess.ec.europa.eu"
LISTING_URL = f"{BASE}/jobs/search"
PAGE_SIZE = 10  # EURAXESS returns 10 results per listing page
PER_SECOND = 0.5  # one request every two seconds — polite default

# Selectors live at the top so a layout change is a two-line edit.
SEL = {
    "result_row": "article.ecl-content-item",
    "detail_link": "h3.ecl-content-block__title a",
    "next_page": "li.ecl-pagination__item--next a",
    "title": "h1.ecl-content-block__title",
    "description": "h2#offer-description",
    "field_term": "dt.ecl-description-list__term",
    "field_def": "dd.ecl-description-list__definition",
    "apply_link": "a.job-apply-button",
    "deadline_time": "time[datetime]",
}

# Regex to extract numeric job ID from EURAXESS detail URLs like /jobs/468285
_JOB_ID_RE = re.compile(r"/jobs/(\d+)")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ListingItem:
    """One entry from a EURAXESS listing page."""

    detail_url: str
    listing_url: str
    title_hint: str
    job_id: str


@dataclass
class RawOpportunity:
    """A fully-fetched EURAXESS opportunity with extracted fields."""

    job_id: str
    title: str
    fields: dict[str, str]
    description: str
    application_url: str | None
    deadline_iso: str | None
    detail_url: str
    listing_url: str
    fetch_id: uuid.UUID
    content_digest: str
    document_text: str
    field_positions: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Set True when a conditional GET returned 304 for this item's detail URL.
    # run.py will update last_verified_at on existing assertions and skip
    # creating new SourceDocument / FieldAssertion records.
    not_modified: bool = False


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _extract_job_id(url: str) -> str:
    """Extract the numeric job ID from a EURAXESS URL."""
    m = _JOB_ID_RE.search(url)
    return m.group(1) if m else ""


def _parse_fields(tree: HTMLParser) -> dict[str, str]:
    """Extract label→value pairs from ECL description lists."""
    fields: dict[str, str] = {}

    terms = tree.css(SEL["field_term"])
    defs = tree.css(SEL["field_def"])

    for term_node, def_node in zip(terms, defs, strict=False):
        label = (term_node.text(strip=True) or "").strip()
        value = (def_node.text(strip=True) or "").strip()
        if label and value:
            fields[label] = value

    return fields


def _parse_deadline(tree: HTMLParser) -> str | None:
    """Extract ISO 8601 deadline from <time datetime="..."> element."""
    time_el = tree.css_first(SEL["deadline_time"])
    if time_el is None:
        return None
    return time_el.attributes.get("datetime")


def parse_listing_page(html: str, listing_url: str) -> list[ListingItem]:
    """Parse a EURAXESS listing page and return ListingItems."""
    tree = HTMLParser(html)
    items: list[ListingItem] = []

    for row in tree.css(SEL["result_row"]):
        link = row.css_first(SEL["detail_link"])
        if link is None:
            continue

        href = link.attributes.get("href", "")
        if not href:
            continue

        # Resolve relative URLs
        detail_url = canonicalize(urljoin(BASE, href))
        title_hint = (link.text(strip=True) or "").strip()
        job_id = _extract_job_id(href)

        if not job_id:
            log.warning("euraxess.listing.no_job_id", href=href)
            continue

        items.append(
            ListingItem(
                detail_url=detail_url,
                listing_url=canonicalize(listing_url),
                title_hint=title_hint,
                job_id=job_id,
            )
        )

    return items


def parse_detail_page(
    html: str,
    detail_url: str,
    listing_url: str,
    job_id: str,
    fetch_id: uuid.UUID,
    content_digest: str,
) -> RawOpportunity:
    """Parse a EURAXESS detail page into a RawOpportunity."""
    tree = HTMLParser(html)

    # Title
    title_el = tree.css_first(SEL["title"])
    title = (title_el.text(strip=True) or "").strip() if title_el else ""

    # Description — h2#offer-description heading, then sibling div.ecl has the text
    desc_heading = tree.css_first(SEL["description"])
    raw_description = ""
    if desc_heading is not None and desc_heading.parent is not None:
        desc_body = desc_heading.parent.css_first("div.ecl")
        if desc_body is not None:
            raw_description = (desc_body.text(strip=True) or "").strip()
    description = scrub(raw_description) or ""

    # Structured fields
    fields = _parse_fields(tree)

    # Deadline from <time> element
    deadline_iso = _parse_deadline(tree)

    # Application URL
    apply_el = tree.css_first(SEL["apply_link"])
    application_url = apply_el.attributes.get("href") if apply_el else None

    # Build document_text: main-content text via trafilatura (strips nav/footer/banners).
    # Falls back to body text if trafilatura returns None.
    raw_main = extract_main_text(html, detail_url)
    document_text = scrub(raw_main) or ""

    # Compute field positions in document_text for grounding
    field_positions: dict[str, tuple[int, int]] = {}
    for label, value in fields.items():
        start = document_text.find(value)
        if start >= 0:
            field_positions[label] = (start, start + len(value))

    return RawOpportunity(
        job_id=job_id,
        title=title,
        fields=fields,
        description=description,
        application_url=application_url,
        deadline_iso=deadline_iso,
        detail_url=detail_url,
        listing_url=listing_url,
        fetch_id=fetch_id,
        content_digest=content_digest,
        document_text=document_text,
        field_positions=field_positions,
    )


def _next_page_url(html: str) -> str | None:
    """Extract the next-page URL from a listing page, or None if last page."""
    tree = HTMLParser(html)
    nxt = tree.css_first(SEL["next_page"])
    if nxt is None:
        return None
    href = nxt.attributes.get("href", "")
    if not href:
        return None
    return canonicalize(urljoin(BASE, href))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _listing_url(page: int) -> str:
    """Build the listing URL for a given 0-indexed page number."""
    return f"{LISTING_URL}?page={page}"


def harvest_from_sitemap(
    source_id: uuid.UUID,
    sitemap_url: str,
    *,
    session: Session | None = None,
) -> Iterator[RawOpportunity]:
    """Discover job URLs from a sitemap instead of paginating listing pages.

    Fetches and parses the sitemap, then fetches each job detail page and
    yields a :class:`RawOpportunity`.  Skips URLs whose ``<lastmod>`` is on
    or before ``CrawlUrl.last_fetched_at`` (i.e. we have a fresh copy).

    This function is an alternative to :func:`harvest` for sources that
    publish a sitemap.  It reuses all of the existing detail-page parsing
    logic — only URL discovery changes.

    Args:
        source_id: UUID of the EURAXESS source row.
        sitemap_url: URL of the root sitemap (e.g. the sitemap.xml URL).
        session: Optional database session for lastmod-based skip checks.
    """
    from aoi.pipeline.fetch_cache import get_last_fetched
    from aoi.pipeline.sitemap import fetch_sitemap

    entries = fetch_sitemap(sitemap_url, source_id, per_second=PER_SECOND)
    log.info("euraxess.sitemap.discovered", count=len(entries), sitemap_url=sitemap_url)

    for entry in entries:
        # Skip if sitemap says the page hasn't changed since our last fetch
        if entry.lastmod is not None and session is not None:
            last_fetched = get_last_fetched(session, entry.url + "?x=1")
            if last_fetched is not None and entry.lastmod <= last_fetched:
                log.debug(
                    "euraxess.sitemap.skip_stale",
                    url=entry.url,
                    lastmod=entry.lastmod.isoformat(),
                    last_fetched=last_fetched.isoformat(),
                )
                continue

        job_id = _extract_job_id(entry.url)
        if not job_id:
            log.warning("euraxess.sitemap.no_job_id", url=entry.url)
            continue

        fetch_url = entry.url + "?x=1"

        # Conditional GET cache
        cached_etag: str | None = None
        cached_last_modified: str | None = None
        if session is not None:
            from aoi.pipeline.fetch_cache import get_cache

            cached_etag, cached_last_modified = get_cache(session, fetch_url)

        try:
            detail_result = fetch(
                fetch_url,
                per_second=PER_SECOND,
                accept="text/html",
                etag=cached_etag,
                last_modified=cached_last_modified,
            )
        except Exception:
            log.exception("euraxess.sitemap.detail_error", url=entry.url)
            continue

        if detail_result.not_modified:
            log.info("euraxess.sitemap.detail_not_modified", url=entry.url, job_id=job_id)
            yield RawOpportunity(
                job_id=job_id,
                title="",
                fields={},
                description="",
                application_url=None,
                deadline_iso=None,
                detail_url=entry.url,
                listing_url=sitemap_url,
                fetch_id=uuid.UUID(int=0),
                content_digest="",
                document_text="",
                not_modified=True,
            )
            continue

        detail_fetched_at = datetime.now(UTC)
        detail_fetch_id = uuid.uuid4()

        evidence = store_fetch(
            source_id=str(source_id),
            fetch_id=str(detail_fetch_id),
            url=fetch_url,
            request_headers={"Accept": "text/html"},
            status=detail_result.status,
            response_headers=detail_result.headers,
            body=detail_result.body,
            fetched_at=detail_fetched_at,
        )

        if session is not None:
            from aoi.pipeline.fetch_cache import set_cache

            set_cache(
                session,
                fetch_url,
                detail_result.headers.get("etag"),
                detail_result.headers.get("last-modified"),
            )

        if detail_result.status != 200:
            log.warning(
                "euraxess.sitemap.detail_bad_status",
                url=entry.url,
                status=detail_result.status,
            )
            continue

        detail_html = detail_result.body.decode("utf-8", errors="replace")
        opportunity = parse_detail_page(
            html=detail_html,
            detail_url=entry.url,
            listing_url=sitemap_url,
            job_id=job_id,
            fetch_id=uuid.UUID(str(evidence.fetch_id)),
            content_digest=evidence.content_digest,
        )

        yield opportunity


def harvest(
    source_id: uuid.UUID,
    *,
    max_pages: int = 1000,
    known_ids: frozenset[str] | None = None,
    early_stop_consecutive: int = 10,
    session: Session | None = None,
) -> Iterator[RawOpportunity]:
    """Walk EURAXESS listing pages, fetch detail pages, yield RawOpportunity.

    Early-stop: when ``early_stop_consecutive`` listing items in a row are
    already in ``known_ids``, pagination halts. This avoids re-crawling
    the full archive on every run. The counter persists across pages.

    Conditional GET: when ``session`` is provided, each detail-page fetch
    sends If-None-Match / If-Modified-Since from the CrawlUrl cache.  A 304
    response yields a ``RawOpportunity`` with ``not_modified=True`` so that
    run.py can update ``last_verified_at`` on existing assertions without
    creating new SourceDocument / FieldAssertion records.

    Args:
        source_id: UUID of the EURAXESS source row.
        max_pages: Maximum listing pages to fetch.
        known_ids: Set of job IDs already in the database — used for early-stop.
        early_stop_consecutive: Number of consecutive known items that triggers
            early stop. Default 10.
        session: Optional database session for conditional GET cache lookups.
    """
    if known_ids is None:
        known_ids = frozenset()

    consecutive_known = 0
    early_stopped = False

    for page in range(0, max_pages):
        url = _listing_url(page)
        log.info("euraxess.harvest.page", page=page, url=url)

        result = fetch(url, per_second=PER_SECOND, accept="text/html")

        fetched_at = datetime.now(UTC)
        listing_fetch_id = uuid.uuid4()

        store_fetch(
            source_id=str(source_id),
            fetch_id=str(listing_fetch_id),
            url=url,
            request_headers={"Accept": "text/html"},
            status=result.status,
            response_headers=result.headers,
            body=result.body,
            fetched_at=fetched_at,
        )

        if result.status != 200:
            log.warning("euraxess.harvest.bad_status", page=page, status=result.status)
            break

        html = result.body.decode("utf-8", errors="replace")
        items = parse_listing_page(html, url)

        if not items:
            log.info("euraxess.harvest.done", page=page, reason="no_items")
            break

        log.info("euraxess.harvest.items", page=page, count=len(items))

        # Fetch each detail page, tracking consecutive known for early-stop
        for item in items:
            if item.job_id in known_ids:
                consecutive_known += 1
                log.debug("euraxess.harvest.skip_known", job_id=item.job_id)
                if consecutive_known >= early_stop_consecutive:
                    log.info(
                        "euraxess.harvest.early_stop",
                        page=page,
                        consecutive=consecutive_known,
                    )
                    early_stopped = True
                    break
                continue

            consecutive_known = 0

            # Append ?x=1 to bypass EURAXESS 307 redirect on bare /jobs/{id}
            fetch_url = item.detail_url + "?x=1"

            # Conditional GET: read cached etag/last_modified if session available
            cached_etag: str | None = None
            cached_last_modified: str | None = None
            if session is not None:
                from aoi.pipeline.fetch_cache import get_cache

                cached_etag, cached_last_modified = get_cache(session, fetch_url)

            try:
                detail_result = fetch(
                    fetch_url,
                    per_second=PER_SECOND,
                    accept="text/html",
                    etag=cached_etag,
                    last_modified=cached_last_modified,
                )
            except Exception:
                log.exception("euraxess.harvest.detail_error", url=item.detail_url)
                continue

            # Handle 304 Not Modified: yield a sentinel so run.py can update
            # last_verified_at on existing assertions without creating new records.
            if detail_result.not_modified:
                log.info(
                    "euraxess.harvest.detail_not_modified",
                    url=item.detail_url,
                    job_id=item.job_id,
                )
                yield RawOpportunity(
                    job_id=item.job_id,
                    title="",
                    fields={},
                    description="",
                    application_url=None,
                    deadline_iso=None,
                    detail_url=item.detail_url,
                    listing_url=item.listing_url,
                    fetch_id=uuid.UUID(int=0),
                    content_digest="",
                    document_text="",
                    not_modified=True,
                )
                continue

            detail_fetched_at = datetime.now(UTC)
            detail_fetch_id = uuid.uuid4()

            evidence = store_fetch(
                source_id=str(source_id),
                fetch_id=str(detail_fetch_id),
                url=fetch_url,
                request_headers={"Accept": "text/html"},
                status=detail_result.status,
                response_headers=detail_result.headers,
                body=detail_result.body,
                fetched_at=detail_fetched_at,
            )

            # Update conditional GET cache with new headers from successful fetch
            if session is not None:
                from aoi.pipeline.fetch_cache import set_cache

                set_cache(
                    session,
                    fetch_url,
                    detail_result.headers.get("etag"),
                    detail_result.headers.get("last-modified"),
                )

            if detail_result.status != 200:
                log.warning(
                    "euraxess.harvest.detail_bad_status",
                    url=item.detail_url,
                    status=detail_result.status,
                )
                continue

            detail_html = detail_result.body.decode("utf-8", errors="replace")
            opportunity = parse_detail_page(
                html=detail_html,
                detail_url=item.detail_url,
                listing_url=item.listing_url,
                job_id=item.job_id,
                fetch_id=uuid.UUID(str(evidence.fetch_id)),
                content_digest=evidence.content_digest,
            )

            yield opportunity

        if early_stopped:
            break

        # Check if there is a next page
        if _next_page_url(html) is None:
            log.info("euraxess.harvest.done", page=page, reason="no_next_page")
            break
    else:
        log.info("euraxess.harvest.done", page=max_pages - 1, reason="max_pages_reached")
