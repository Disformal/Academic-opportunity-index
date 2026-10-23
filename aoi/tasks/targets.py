"""Crawl targets found by adapters (discover, listings): find or create, with their host.

Two workers may add the same new URL, or a URL on the same new host, at once. Each insert skips a
row that already exists (waiting for one another transaction holds), and the read that follows
sees the committed row.
"""

import hashlib
import uuid
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aoi.adapters.protocol import CrawlTargetSpec
from aoi.db.schema.ops import CrawlTarget, Host
from aoi.pipeline.canonicalize import canonicalize


def add_target(session: Session, source_id: uuid.UUID, spec: CrawlTargetSpec) -> CrawlTarget:
    """The source's target for the spec's canonical URL; a new one is due at once."""
    url = canonicalize(spec.url)
    url_sha256 = hashlib.sha256(url.encode()).hexdigest()
    session.execute(
        insert(CrawlTarget)
        .values(
            source_id=source_id,
            host_id=_host(session, urlsplit(url).hostname or "").id,
            url=url,
            url_sha256=url_sha256,
            role=spec.role,
        )
        .on_conflict_do_nothing(constraint="uq_crawl_target_source_url")
    )
    return session.scalars(
        select(CrawlTarget).where(
            CrawlTarget.source_id == source_id, CrawlTarget.url_sha256 == url_sha256
        )
    ).one()


def _host(session: Session, hostname: str) -> Host:
    session.execute(
        insert(Host)
        .values(hostname=hostname)
        .on_conflict_do_nothing(constraint="uq_host_hostname")
    )
    return session.scalars(select(Host).where(Host.hostname == hostname)).one()
