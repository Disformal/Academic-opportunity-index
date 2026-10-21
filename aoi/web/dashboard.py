"""Internal health dashboard — per-source metrics, aggregates, evidence stats."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from aoi.db.enums import FetchOutcome, OpportunityStatus
from aoi.db.models import (
    DuplicateCluster,
    Fetch,
    FieldAssertion,
    Opportunity,
    Source,
    SourceDocument,
)
from aoi.web.deps import get_db, limiter

log = structlog.get_logger()

_TEMPLATES_DIR = __import__("pathlib").Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


@dataclass
class SourceHealth:
    """Health metrics for a single source."""

    source: Any  # Source model instance
    records_available: int
    records_total: int
    last_crawl_at: datetime | None
    consecutive_failures: int
    score: int


def _compute_health_score(
    *,
    records_available: int,
    records_total: int,
    last_crawl_at: datetime | None,
    consecutive_failures: int,
) -> int:
    """Compute a 0–100 health score for a source."""
    availability = records_available / max(records_total, 1)

    recency_penalty = 0
    if last_crawl_at is not None:
        age = datetime.now(UTC) - last_crawl_at
        if age > timedelta(days=7):
            recency_penalty = 50
        elif age > timedelta(days=2):
            recency_penalty = 25
    else:
        recency_penalty = 50

    failure_penalty = min(consecutive_failures * 10, 50)
    return max(0, round(availability * 100 - recency_penalty - failure_penalty))


def _source_health_list(db: Session) -> list[SourceHealth]:
    """Build health metrics for every enabled source."""
    sources = db.execute(select(Source).where(Source.enabled.is_(True))).scalars().all()
    result: list[SourceHealth] = []

    for src in sources:
        # Opportunity counts via provenance chain
        opp_base = (
            select(Opportunity.id)
            .join(FieldAssertion, FieldAssertion.opportunity_id == Opportunity.id)
            .join(SourceDocument, FieldAssertion.source_document_id == SourceDocument.id)
            .where(SourceDocument.source_id == src.id, FieldAssertion.is_current.is_(True))
            .distinct()
        )

        records_total: int = (
            db.execute(select(func.count()).select_from(opp_base.subquery())).scalar() or 0
        )

        records_available: int = (
            db.execute(
                select(func.count()).select_from(
                    opp_base.where(Opportunity.status != OpportunityStatus.CLOSED).subquery()
                )
            ).scalar()
            or 0
        )

        last_crawl_at = db.execute(
            select(func.max(Fetch.fetched_at)).where(Fetch.source_id == src.id)
        ).scalar()

        # Consecutive failures: count ERROR outcomes from most recent fetches
        recent_fetches = (
            db.execute(
                select(Fetch.outcome)
                .where(Fetch.source_id == src.id)
                .order_by(Fetch.fetched_at.desc())
                .limit(20)
            )
            .scalars()
            .all()
        )
        consecutive_failures = 0
        for outcome in recent_fetches:
            if outcome == FetchOutcome.ERROR:
                consecutive_failures += 1
            else:
                break

        score = _compute_health_score(
            records_available=records_available,
            records_total=records_total,
            last_crawl_at=last_crawl_at,
            consecutive_failures=consecutive_failures,
        )

        result.append(
            SourceHealth(
                source=src,
                records_available=records_available,
                records_total=records_total,
                last_crawl_at=last_crawl_at,
                consecutive_failures=consecutive_failures,
                score=score,
            )
        )

    return result


def _aggregate_stats(db: Session) -> dict[str, Any]:
    """Compute aggregate statistics across all opportunities."""
    total: int = db.execute(select(func.count(Opportunity.id))).scalar() or 0

    open_count: int = (
        db.execute(
            select(func.count(Opportunity.id)).where(
                Opportunity.status != OpportunityStatus.CLOSED
            )
        ).scalar()
        or 0
    )

    closed_count: int = (
        db.execute(
            select(func.count(Opportunity.id)).where(
                Opportunity.status == OpportunityStatus.CLOSED
            )
        ).scalar()
        or 0
    )

    # Opportunities by type
    by_type: list[tuple[str, int]] = list(
        db.execute(
            select(Opportunity.type, func.count(Opportunity.id))
            .group_by(Opportunity.type)
            .order_by(func.count(Opportunity.id).desc())
        ).all()
    )

    # Dedup stats
    cluster_count: int = db.execute(select(func.count(DuplicateCluster.id))).scalar() or 0
    dedup_ratio = cluster_count / max(total, 1)

    # Closure rate by source
    closure_by_source: list[tuple[str, int, int]] = []
    sources = db.execute(select(Source).where(Source.enabled.is_(True))).scalars().all()
    for src in sources:
        src_opps = (
            select(Opportunity.id)
            .join(FieldAssertion, FieldAssertion.opportunity_id == Opportunity.id)
            .join(SourceDocument, FieldAssertion.source_document_id == SourceDocument.id)
            .where(SourceDocument.source_id == src.id, FieldAssertion.is_current.is_(True))
            .distinct()
            .subquery()
        )
        src_total: int = db.execute(select(func.count()).select_from(src_opps)).scalar() or 0
        src_closed: int = (
            db.execute(
                select(func.count(Opportunity.id)).where(
                    Opportunity.id.in_(select(src_opps)),
                    Opportunity.status == OpportunityStatus.CLOSED,
                )
            ).scalar()
            or 0
        )
        if src_total > 0:
            closure_by_source.append((src.name, src_closed, src_total))

    # Fetch outcome breakdown
    fetch_outcomes: list[tuple[str, int]] = list(
        db.execute(
            select(
                case(
                    (Fetch.outcome == FetchOutcome.HIT, "hit"),
                    (Fetch.outcome == FetchOutcome.NOT_MODIFIED, "not_modified"),
                    (Fetch.outcome == FetchOutcome.ERROR, "error"),
                    (Fetch.outcome == FetchOutcome.BLOCKED, "blocked"),
                    (Fetch.outcome == FetchOutcome.RATE_LIMITED, "rate_limited"),
                    else_="other",
                ),
                func.count(Fetch.id),
            )
            .group_by(Fetch.outcome)
            .order_by(func.count(Fetch.id).desc())
        ).all()
    )

    return {
        "total": total,
        "open": open_count,
        "closed": closed_count,
        "by_type": by_type,
        "cluster_count": cluster_count,
        "dedup_ratio": round(dedup_ratio, 4),
        "closure_by_source": closure_by_source,
        "fetch_outcomes": fetch_outcomes,
    }


def _evidence_stats() -> dict[str, Any]:
    """Best-effort evidence store statistics. Gracefully degrades if S3 unreachable."""
    try:
        from aoi.config import get_settings
        from aoi.storage.evidence import _get_s3_client

        client = _get_s3_client()
        settings = get_settings()
        stats: dict[str, Any] = {}

        for bucket_name in [settings.s3_bucket_evidence, settings.s3_bucket_derived]:
            count = 0
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket_name):
                count += page.get("KeyCount", 0)
            stats[bucket_name] = {"object_count": count}

        return stats
    except Exception:
        log.warning("dashboard.evidence_stats_unavailable")
        return {}


@router.get("/dashboard")
@limiter.limit("10/minute")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),  # noqa: B008
) -> Response:
    """Internal health dashboard."""
    source_health = _source_health_list(db)
    aggregates = _aggregate_stats(db)
    evidence = _evidence_stats()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "source_health": source_health,
            "aggregates": aggregates,
            "evidence": evidence,
        },
    )
