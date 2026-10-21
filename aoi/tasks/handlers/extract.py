"""The extract handler (plan §4.3, ADR 0030, 0031): one fetch's WARC through its source's adapter.

parse_listing runs on listing and feed targets: the targets it links to become crawl targets, and
the postings it shows are sighted. parse_detail runs on every document: its postings are found or
created by within-source identity (ADR 0008) and its assertions written with the fetch, the run
and their evidence pointer. Every posting shown gets one sighting per fetch (ADR 0033). A fetch is
extracted once per extractor version (ADR 0030 §2). A posting that gets new assertions gets a
resolve task, unless one is already waiting (a running one may have read before this commit).

An unchanged fetch has no new content: a revisit refers to an earlier capture, and a 304 repeats
the target's latest earlier response. That content is parsed again and its postings sighted at the
unchanged fetch's time; no assertions are written and no targets added (ADR 0033).
"""

import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aoi.adapters.protocol import Adapter, Document, PostingRef
from aoi.db.schema.core import Assertion, ExtractionRun, Posting, PostingSighting, Source
from aoi.db.schema.enums import PostingIdentityKind, TargetRole
from aoi.db.schema.evidence import Fetch
from aoi.db.schema.ops import CrawlTarget, Task
from aoi.pipeline.canonicalize import canonicalize
from aoi.storage.warc import Capture, read_capture
from aoi.tasks.handlers.resolve import queue_resolve
from aoi.tasks.targets import add_target
from aoi.tasks.worker import Handler

log = structlog.get_logger()

_LISTING_ROLES = (TargetRole.LISTING, TargetRole.FEED)


def make_extract_handler(adapters: Mapping[str, Adapter]) -> Handler:
    """An extract handler; a source's adapter is keyed by its platform, or else its name."""

    def handle_extract(session: Session, task: Task) -> None:
        fetch = session.get_one(Fetch, uuid.UUID(task.payload["fetch_id"]))
        target = session.get_one(CrawlTarget, fetch.crawl_target_id)
        source = session.get_one(Source, target.source_id)
        adapter = adapters[source.platform or source.name]
        if _already_extracted(session, fetch.id, adapter):
            log.info("extract.skipped", fetch_id=str(fetch.id), extractor=adapter.name)
            return

        run = ExtractionRun(
            fetch_id=fetch.id,
            extractor=adapter.name,
            extractor_version=adapter.version,
            started_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()
        assert fetch.warc_key is not None, "extract tasks are queued only for stored responses"
        capture = read_capture(fetch.warc_key)
        if capture.record_type == "revisit" or capture.status == 304:
            doc = Document.from_capture(_earlier_content(session, fetch, capture))
            refs = adapter.parse_listing(doc).refs if target.role in _LISTING_ROLES else ()
            refs += tuple(item.ref for item in adapter.parse_detail(doc))
            for ref in refs:
                _sighted(session, source.id, ref, fetch)
            run.finished_at = datetime.now(UTC)
            log.info("extract.unchanged", fetch_id=str(fetch.id), sightings=len(set(refs)))
            return
        doc = Document.from_capture(capture)

        if target.role in _LISTING_ROLES:
            page = adapter.parse_listing(doc)
            for spec in page.targets:
                add_target(session, source.id, spec)
            for ref in page.refs:
                _sighted(session, source.id, ref, fetch)
        parsed = adapter.parse_detail(doc)
        for item in parsed:
            posting = _sighted(session, source.id, item.ref, fetch)
            session.add_all(
                Assertion(
                    **asdict(spec),
                    posting_id=posting.id,
                    fetch_id=fetch.id,
                    extraction_run_id=run.id,
                    observed_at=fetch.requested_at,
                )
                for spec in item.assertions
            )
            if item.assertions:
                queue_resolve(session, source.id, posting.id)
        run.finished_at = datetime.now(UTC)
        log.info(
            "extract.done", fetch_id=str(fetch.id), extractor=adapter.name, postings=len(parsed)
        )

    return handle_extract


def _already_extracted(session: Session, fetch_id: uuid.UUID, adapter: Adapter) -> bool:
    return (
        session.scalar(
            select(ExtractionRun.id).where(
                ExtractionRun.fetch_id == fetch_id,
                ExtractionRun.extractor == adapter.name,
                ExtractionRun.extractor_version == adapter.version,
            )
        )
        is not None
    )


def _earlier_content(session: Session, fetch: Fetch, capture: Capture) -> Capture:
    """The response an unchanged fetch repeats: a revisit's referent, or for a 304 the target's
    latest earlier 2xx response (itself possibly a revisit, which is followed)."""
    if capture.record_type == "revisit":
        earlier = session.scalars(
            select(Fetch).where(Fetch.warc_record_id == capture.refers_to)
        ).one()
    else:
        earlier = session.scalars(
            select(Fetch)
            .where(
                Fetch.crawl_target_id == fetch.crawl_target_id,
                Fetch.http_status.between(200, 299),
                Fetch.warc_key.is_not(None),
                Fetch.requested_at < fetch.requested_at,
            )
            .order_by(Fetch.requested_at.desc())
            .limit(1)
        ).one()
    assert earlier.warc_key is not None
    content = read_capture(earlier.warc_key)
    if content.record_type == "revisit":
        return _earlier_content(session, earlier, content)
    return content


def _sighted(session: Session, source_id: uuid.UUID, ref: PostingRef, fetch: Fetch) -> Posting:
    """The posting with this identity, created if new, sighted by `fetch` (once per fetch) and
    its sighting window widened to the fetch's time.

    Detail URLs, and identity keys that are detail URLs, are canonicalized as crawl targets are,
    so two spellings of one URL cannot become two postings.
    """
    at = fetch.requested_at
    key = ref.identity_key
    if ref.identity_kind == PostingIdentityKind.DETAIL_URL:
        key = canonicalize(key)
    posting = session.scalars(
        select(Posting).where(
            Posting.source_id == source_id,
            Posting.identity_kind == ref.identity_kind,
            Posting.identity_key == key,
        )
    ).first()
    if posting is None:
        posting = Posting(
            source_id=source_id,
            identity_kind=ref.identity_kind,
            identity_key=key,
            first_seen_at=at,
            last_seen_at=at,
        )
        session.add(posting)
    posting.first_seen_at = min(posting.first_seen_at, at)
    posting.last_seen_at = max(posting.last_seen_at, at)
    if ref.detail_url is not None:
        posting.detail_url = canonicalize(ref.detail_url)
    if ref.kind is not None:
        posting.kind = ref.kind
    session.flush()
    session.execute(
        insert(PostingSighting)
        .values(posting_id=posting.id, fetch_id=fetch.id, observed_at=at)
        .on_conflict_do_nothing(constraint="uq_posting_sighting_posting_fetch")
    )
    return posting
