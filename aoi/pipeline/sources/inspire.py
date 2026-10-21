"""INSPIRE-HEP jobs API adapter."""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from aoi.pipeline.fetch import fetch
from aoi.pipeline.privacy import scrub
from aoi.storage.evidence import store_fetch

log = structlog.get_logger()

BASE = "https://inspirehep.net/api/jobs"
PAGE_SIZE = 250

FIELDS = ",".join(
    [
        "control_number",
        "position",
        "institutions",
        "regions",
        "ranks",
        "deadline_date",
        "status",
        "arxiv_categories",
        "description",
        "urls",
        "legacy_creation_date",
    ]
)

# Rate: INSPIRE allows ~15 req/5s = 3/s; we use 2/3 of that = 2.0 rps
_INSPIRE_RPS = 2.0


@dataclass
class RawJob:
    control_number: str
    payload: dict[str, Any]
    fetch_id: uuid.UUID
    content_digest: str
    document_text: str
    pointers: dict[str, str] = field(default_factory=dict)


def _page_url(page: int) -> str:
    return f"{BASE}?sort=mostrecent&size={PAGE_SIZE}&page={page}&fields={FIELDS}&q="


def _build_pointers(metadata: dict[str, Any]) -> dict[str, str]:
    """Build JSON pointer strings for extractable fields."""
    pointer_fields = [
        "position",
        "deadline_date",
        "status",
        "institutions",
        "regions",
        "ranks",
        "arxiv_categories",
        "description",
        "urls",
        "legacy_creation_date",
    ]
    return {f: f"/metadata/{f}" for f in pointer_fields if f in metadata}


def harvest(
    source_id: uuid.UUID,
    *,
    max_pages: int = 1000,
    known_keys: frozenset[str] | None = None,
    early_stop_consecutive: int = 10,
    session: Session | None = None,
) -> Iterator[RawJob]:
    """Walk the INSPIRE jobs API, storing evidence for every page.

    Yields a RawJob for each hit. Stops when a page returns fewer hits than
    PAGE_SIZE, when max_pages is reached, or when ``early_stop_consecutive``
    items in a row are already in ``known_keys`` (incremental mode).

    Conditional GET: when ``session`` is provided, each page fetch sends
    If-None-Match / If-Modified-Since from the CrawlUrl cache.  A 304 response
    skips the page entirely — the early-stop / known_keys mechanism already
    handles re-run efficiency for individual items.
    """
    consecutive_known = 0
    early_stopped = False

    for page in range(1, max_pages + 1):
        url = _page_url(page)
        log.info("inspire.harvest.page", page=page, url=url)

        # Conditional GET: read cached headers if session is available
        cached_etag: str | None = None
        cached_last_modified: str | None = None
        if session is not None:
            from aoi.pipeline.fetch_cache import get_cache

            cached_etag, cached_last_modified = get_cache(session, url)

        result = fetch(
            url,
            per_second=_INSPIRE_RPS,
            accept="application/json",
            etag=cached_etag,
            last_modified=cached_last_modified,
        )

        # 304 Not Modified: page content unchanged, skip all items on this page
        if result.not_modified:
            log.info("inspire.harvest.page_not_modified", page=page, url=url)
            continue

        fetched_at = datetime.now(UTC)
        fetch_id = uuid.uuid4()

        evidence = store_fetch(
            source_id=str(source_id),
            fetch_id=str(fetch_id),
            url=url,
            request_headers={"Accept": "application/json"},
            status=result.status,
            response_headers=result.headers,
            body=result.body,
            fetched_at=fetched_at,
        )

        # Update conditional GET cache with headers from this successful fetch
        if session is not None:
            from aoi.pipeline.fetch_cache import set_cache

            set_cache(
                session,
                url,
                result.headers.get("etag"),
                result.headers.get("last-modified"),
            )

        data = json.loads(result.body)
        hits = data.get("hits", {}).get("hits", [])

        log.info("inspire.harvest.hits", page=page, count=len(hits))

        for hit in hits:
            metadata = hit.get("metadata", {})
            control_number = str(metadata.get("control_number", hit.get("id", "")))

            # Track consecutive known items for early-stop
            if known_keys is not None:
                identity_key = f"inspire:{control_number}"
                if identity_key in known_keys:
                    consecutive_known += 1
                    if consecutive_known >= early_stop_consecutive:
                        log.info(
                            "inspire.harvest.early_stop",
                            page=page,
                            consecutive=consecutive_known,
                        )
                        early_stopped = True
                        break
                    continue
                consecutive_known = 0

            # Scrub contact data from description before yielding
            description = metadata.get("description", "")
            scrubbed_description = scrub(description) or ""

            # Build document_text with scrubbed description
            scrubbed_metadata = dict(metadata)
            scrubbed_metadata["description"] = scrubbed_description
            scrubbed_hit = dict(hit)
            scrubbed_hit["metadata"] = scrubbed_metadata
            document_text = json.dumps(scrubbed_hit, ensure_ascii=False)

            pointers = _build_pointers(metadata)

            yield RawJob(
                control_number=control_number,
                payload=scrubbed_metadata,
                fetch_id=uuid.UUID(str(evidence.fetch_id)),
                content_digest=evidence.content_digest,
                document_text=document_text,
                pointers=pointers,
            )

        if early_stopped:
            break

        # Stop if this page was not full
        if len(hits) < PAGE_SIZE:
            log.info("inspire.harvest.done", page=page, reason="partial_page")
            break
    else:
        log.info("inspire.harvest.done", page=max_pages, reason="max_pages_reached")
