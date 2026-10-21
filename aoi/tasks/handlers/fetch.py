"""The fetch handler (plan §4.3, ADR 0030): one request for one crawl target.

The WARC object is stored before the immutable evidence.fetch row (ADR 0027). A body identical to
the target's last one is stored as a revisit. Every 2xx and 304 is queued for extraction; an
unchanged one only records which postings it showed (ADR 0033). The recrawl interval halves
when the content changed and doubles otherwise, within [1 h, 7 d]. A request without a response,
a 5xx and a 429 are recorded as fetch rows and fail the attempt (AttemptFailed keeps the row).
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import NoReturn

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aoi.db.schema.enums import FetchOutcome
from aoi.db.schema.evidence import Fetch
from aoi.db.schema.ops import CrawlTarget, Task
from aoi.pipeline.canonicalize import BlockedAddress, ResolutionFailed
from aoi.pipeline.fetch import BodyTooLarge, fetch
from aoi.storage.warc import EarlierCapture, Exchange, store_exchange
from aoi.tasks.worker import AttemptFailed

log = structlog.get_logger()

MIN_INTERVAL = timedelta(hours=1)
MAX_INTERVAL = timedelta(days=7)


def next_interval(current: timedelta, *, changed: bool) -> timedelta:
    """Halve on change, double otherwise, within [MIN_INTERVAL, MAX_INTERVAL]."""
    proposed = current / 2 if changed else current * 2
    return min(max(proposed, MIN_INTERVAL), MAX_INTERVAL)


def handle_fetch(session: Session, task: Task) -> None:
    target = session.scalars(
        select(CrawlTarget).where(CrawlTarget.id == task.crawl_target_id).with_for_update()
    ).one()
    fetch_id = uuid.uuid4()
    requested_at = datetime.now(UTC)
    try:
        # No 429 retries here: sleeping in the worker could outlast the lease; the queue backs off.
        result = fetch(
            target.url, etag=target.etag, last_modified=target.last_modified, max_429_retries=0
        )
    except ResolutionFailed as exc:
        _record_no_response(session, fetch_id, target, requested_at, FetchOutcome.ERROR, exc)
    except BlockedAddress as exc:
        _record_no_response(session, fetch_id, target, requested_at, FetchOutcome.BLOCKED, exc)
    except (httpx.HTTPError, BodyTooLarge) as exc:
        _record_no_response(session, fetch_id, target, requested_at, FetchOutcome.ERROR, exc)

    succeeded = 200 <= result.status < 300
    sha = hashlib.sha256(result.body).hexdigest() if result.status != 304 else None
    changed = succeeded and sha != target.content_sha256
    earlier = None if changed or not succeeded else _first_capture(session, target.id, sha)
    stored = store_exchange(
        fetch_id,
        Exchange(
            url=result.final_url,
            request_headers=result.request_headers,
            http_version=result.http_version,
            status=result.status,
            response_headers=result.headers,
            body=result.body,
            fetched_at=requested_at,
        ),
        same_as=earlier,
    )
    outcome = _outcome(result.status)
    session.add(
        Fetch(
            id=fetch_id,
            crawl_target_id=target.id,
            url=target.url,
            final_url=result.final_url,
            requested_at=requested_at,
            elapsed_ms=result.elapsed_ms,
            outcome=outcome,
            http_status=result.status,
            resolved_ip=result.resolved_ip,
            body_sha256=sha,
            body_bytes=len(result.body),
            warc_record_id=stored.record_id,
            warc_key=stored.key,
        )
    )
    if outcome in (FetchOutcome.ERROR, FetchOutcome.RATE_LIMITED):
        session.flush()
        raise AttemptFailed(f"HTTP {result.status} from {result.final_url}")

    if succeeded:
        headers = {name.lower(): value for name, value in result.headers.items()}
        target.etag = headers.get("etag")
        target.last_modified = headers.get("last-modified")
        target.content_sha256 = sha
    target.recrawl_interval = next_interval(target.recrawl_interval, changed=changed)
    target.next_due_at = func.now() + target.recrawl_interval
    if succeeded or result.status == 304:
        session.add(
            Task(kind="extract", source_id=task.source_id, payload={"fetch_id": str(fetch_id)})
        )
    log.info(
        "fetch.recorded", target=target.url, status=result.status, changed=changed, key=stored.key
    )


def _outcome(status: int) -> FetchOutcome:
    if status == 304:
        return FetchOutcome.NOT_MODIFIED
    if status == 429:
        return FetchOutcome.RATE_LIMITED
    if status >= 500:
        return FetchOutcome.ERROR
    return FetchOutcome.HIT


def _first_capture(session: Session, target_id: uuid.UUID, sha: str | None) -> EarlierCapture:
    """The first successful fetch of this body: always a full response, never a revisit.

    It exists whenever the body matches the target's content_sha256, because only a successful
    fetch sets that hash, and it inserts this row with its WARC.
    """
    first = session.scalars(
        select(Fetch)
        .where(
            Fetch.crawl_target_id == target_id,
            Fetch.body_sha256 == sha,
            Fetch.http_status.between(200, 299),
            Fetch.warc_record_id.is_not(None),
        )
        .order_by(Fetch.requested_at)
        .limit(1)
    ).one()
    return EarlierCapture(
        record_id=str(first.warc_record_id),
        url=first.final_url or first.url,
        fetched_at=first.requested_at,
    )


def _record_no_response(
    session: Session,
    fetch_id: uuid.UUID,
    target: CrawlTarget,
    requested_at: datetime,
    outcome: FetchOutcome,
    exc: Exception,
) -> NoReturn:
    """Record a request that got no response, then fail the attempt (the row is kept)."""
    error = f"{type(exc).__name__}: {exc}"
    target.next_due_at = func.now() + target.recrawl_interval
    session.add(
        Fetch(
            id=fetch_id,
            crawl_target_id=target.id,
            url=target.url,
            requested_at=requested_at,
            outcome=outcome,
            error=error,
        )
    )
    session.flush()
    raise AttemptFailed(error) from exc
