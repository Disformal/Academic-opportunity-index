"""Replay (ADR 0006): re-extract a source's stored evidence with the current adapter version.

Every stored 2xx or 304 response of the source that this adapter version has not extracted gets an
extract task; workers then run them like any other. Revisits and 304s are included and record only
sightings (ADR 0033). A second replay with the same version queues nothing.
"""

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aoi.adapters.protocol import Adapter
from aoi.db.schema.core import ExtractionRun, Source
from aoi.db.schema.evidence import Fetch
from aoi.db.schema.ops import CrawlTarget, Task

log = structlog.get_logger()


def queue_replay(session: Session, source: Source, adapter: Adapter) -> int:
    """Queue the extract tasks in the caller's transaction; return how many."""
    extracted = (
        select(ExtractionRun.id)
        .where(
            ExtractionRun.fetch_id == Fetch.id,
            ExtractionRun.extractor == adapter.name,
            ExtractionRun.extractor_version == adapter.version,
        )
        .exists()
    )
    fetch_ids = session.scalars(
        select(Fetch.id)
        .join(CrawlTarget, CrawlTarget.id == Fetch.crawl_target_id)
        .where(
            CrawlTarget.source_id == source.id,
            or_(Fetch.http_status.between(200, 299), Fetch.http_status == 304),
            Fetch.warc_key.is_not(None),
            ~extracted,
        )
        .order_by(Fetch.requested_at)
    ).all()
    session.add_all(
        Task(kind="extract", source_id=source.id, payload={"fetch_id": str(fetch_id)})
        for fetch_id in fetch_ids
    )
    session.flush()
    log.info(
        "replay.queued",
        source=source.name,
        extractor=adapter.name,
        version=adapter.version,
        tasks=len(fetch_ids),
    )
    return len(fetch_ids)
