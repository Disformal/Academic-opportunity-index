"""Conditional GET cache — read/write per-URL ETags and Last-Modified headers.

Uses the existing CrawlUrl table as backing store: CrawlUrl.etag and
CrawlUrl.last_modified already exist for this purpose.  No separate table
is needed.

Public API
----------
get_cache(session, url)          → (etag, last_modified) or (None, None)
set_cache(session, url, ...)     → update etag/last_modified on CrawlUrl
get_last_fetched(session, url)   → last_fetched_at datetime or None
touch_verified_at(session, ...)  → update last_verified_at on FieldAssertions
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.orm import Session

from aoi.db.models import CrawlUrl, FieldAssertion
from aoi.pipeline.canonicalize import canonicalize, url_hash

log = structlog.get_logger()


def get_cache(session: Session, url: str) -> tuple[str | None, str | None]:
    """Return (etag, last_modified) for url from CrawlUrl, or (None, None).

    Uses the canonical URL and its hash for the lookup so that minor URL
    variations (trailing slash, query-param order) resolve to the same row.
    """
    canonical = canonicalize(url)
    h = url_hash(canonical)
    row = session.query(CrawlUrl).filter_by(url_hash=h).first()
    if row is None:
        return None, None
    return row.etag, row.last_modified


def set_cache(
    session: Session,
    url: str,
    etag: str | None,
    last_modified: str | None,
) -> None:
    """Write etag and last_modified for url back to the CrawlUrl row.

    If the CrawlUrl does not exist yet (first fetch of this URL), this is a
    no-op — _ensure_crawl_url in run.py will create it later in the same
    transaction.  We do not create a CrawlUrl here because that would bypass
    the source_id association and PageRole assignment in run.py.
    """
    canonical = canonicalize(url)
    h = url_hash(canonical)
    row = session.query(CrawlUrl).filter_by(url_hash=h).first()
    if row is None:
        # CrawlUrl not yet created for this URL; it will be created by
        # _ensure_crawl_url on the same run.  Cache is effectively empty
        # until after the first successful fetch is persisted.
        log.debug("fetch_cache.set.no_crawl_url", url=url)
        return

    row.etag = etag
    row.last_modified = last_modified
    row.last_fetched_at = datetime.now(UTC)
    log.debug(
        "fetch_cache.set",
        url=url,
        etag=etag,
        last_modified=last_modified,
    )


def get_last_fetched(session: Session, url: str) -> datetime | None:
    """Return last_fetched_at for url from CrawlUrl, or None if not yet fetched.

    Used by the sitemap harvester to compare a sitemap's <lastmod> against
    when we last fetched the URL.  If lastmod <= last_fetched_at the URL has
    not changed since our last fetch and can be skipped.
    """
    canonical = canonicalize(url)
    h = url_hash(canonical)
    row = session.query(CrawlUrl).filter_by(url_hash=h).first()
    if row is None:
        return None
    return row.last_fetched_at


def touch_verified_at(
    session: Session,
    source_document_id: uuid.UUID,
) -> int:
    """Set last_verified_at = now() on all current FieldAssertions for a SourceDocument.

    Called when a conditional GET returns 304: the source document has not
    changed, so existing assertions are still valid.  Updating last_verified_at
    prevents the closure detector from treating them as stale.

    Returns the number of rows updated.
    """
    now = datetime.now(UTC)
    updated = (
        session.query(FieldAssertion)
        .filter(
            FieldAssertion.source_document_id == source_document_id,
            FieldAssertion.is_current.is_(True),
        )
        .update({"last_verified_at": now}, synchronize_session="fetch")
    )
    log.debug(
        "fetch_cache.touch_verified_at",
        source_document_id=str(source_document_id),
        rows_updated=updated,
    )
    return updated
