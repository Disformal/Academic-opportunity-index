"""Crawl targets found by adapters (discover, listings): find or create, with their host."""

import hashlib
import uuid
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from aoi.adapters.protocol import CrawlTargetSpec
from aoi.db.schema.ops import CrawlTarget, Host
from aoi.pipeline.canonicalize import canonicalize


def add_target(session: Session, source_id: uuid.UUID, spec: CrawlTargetSpec) -> CrawlTarget:
    """The source's target for the spec's canonical URL; a new one is due at once."""
    url = canonicalize(spec.url)
    url_sha256 = hashlib.sha256(url.encode()).hexdigest()
    target = session.scalars(
        select(CrawlTarget).where(
            CrawlTarget.source_id == source_id, CrawlTarget.url_sha256 == url_sha256
        )
    ).first()
    if target is None:
        target = CrawlTarget(
            source_id=source_id,
            host_id=_host(session, urlsplit(url).hostname or "").id,
            url=url,
            url_sha256=url_sha256,
            role=spec.role,
        )
        session.add(target)
        session.flush()
    return target


def _host(session: Session, hostname: str) -> Host:
    host = session.scalars(select(Host).where(Host.hostname == hostname)).first()
    if host is None:
        host = Host(hostname=hostname)
        session.add(host)
        session.flush()
    return host
