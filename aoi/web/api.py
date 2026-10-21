"""Pipeline control API — trigger runs, list runs, manage sources."""

import subprocess
import sys
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from aoi.db.models import PipelineRun, Source
from aoi.web.deps import get_db, limiter

log = structlog.get_logger()

router = APIRouter(prefix="/api")


@router.post("/runs")
@limiter.limit("5/minute")
def trigger_run(
    request: Request,
    source_name: str = Query(..., max_length=200),
    db: Session = Depends(get_db),  # noqa: B008
) -> dict[str, str]:
    """Trigger a pipeline run for the given source.

    Creates a PipelineRun row, then spawns a detached subprocess.
    """
    source = db.query(Source).filter_by(name=source_name, enabled=True).first()
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found or not enabled")

    # Check for already-running run on this source
    running = db.query(PipelineRun).filter_by(source_id=source.id, status="running").first()
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Source {source_name!r} already has a running pipeline (run_id={running.id})",
        )

    # Create pipeline_run row
    run = PipelineRun(
        source_id=source.id,
        started_at=datetime.now(UTC),
        status="running",
        triggered_by="api",
    )
    db.add(run)
    db.flush()
    run_id = str(run.id)

    # Spawn detached subprocess
    subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "aoi.pipeline.run",
            "--source",
            source_name,
            "--run-id",
            run_id,
        ],
        start_new_session=True,
    )

    log.info("api.run.triggered", source=source_name, run_id=run_id)
    return {"run_id": run_id, "status": "started"}


@router.get("/runs")
@limiter.limit("30/minute")
def list_runs(
    request: Request,
    source_name: str = Query(default="", max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),  # noqa: B008
) -> list[dict[str, str | int | None]]:
    """List recent pipeline runs, optionally filtered by source."""
    stmt = (
        select(PipelineRun, Source.name)
        .join(Source, PipelineRun.source_id == Source.id)
        .order_by(PipelineRun.started_at.desc())
        .limit(limit)
    )
    if source_name.strip():
        stmt = stmt.where(Source.name == source_name.strip())

    rows = db.execute(stmt).all()
    return [
        {
            "run_id": str(run.id),
            "source": src_name,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "status": run.status,
            "created": run.created,
            "updated": run.updated,
            "unchanged": run.unchanged,
            "errors": run.errors,
            "closed": run.closed,
            "quarantined": run.quarantined,
            "triggered_by": run.triggered_by,
            "error_message": run.error_message,
        }
        for run, src_name in rows
    ]


@router.get("/runs/{run_id}")
@limiter.limit("60/minute")
def get_run(
    request: Request,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
) -> dict[str, str | int | None]:
    """Get details for a specific pipeline run."""
    row = db.execute(
        select(PipelineRun, Source.name)
        .join(Source, PipelineRun.source_id == Source.id)
        .where(PipelineRun.id == run_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")

    run, src_name = row
    return {
        "run_id": str(run.id),
        "source": src_name,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "status": run.status,
        "created": run.created,
        "updated": run.updated,
        "unchanged": run.unchanged,
        "errors": run.errors,
        "closed": run.closed,
        "quarantined": run.quarantined,
        "triggered_by": run.triggered_by,
        "error_message": run.error_message,
    }


@router.get("/sources")
@limiter.limit("30/minute")
def list_sources(
    request: Request,
    db: Session = Depends(get_db),  # noqa: B008
) -> list[dict[str, str | bool | float | None]]:
    """List all sources with their status."""
    sources = db.execute(select(Source).order_by(Source.name)).scalars().all()
    return [
        {
            "id": str(s.id),
            "name": s.name,
            "domain": s.domain,
            "source_type": s.source_type,
            "enabled": s.enabled,
            "last_harvested_at": s.last_harvested_at.isoformat() if s.last_harvested_at else None,
            "requests_per_second": float(s.requests_per_second),
        }
        for s in sources
    ]


@router.post("/sources/{source_id}/toggle")
@limiter.limit("10/minute")
def toggle_source(
    request: Request,
    source_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
) -> dict[str, str | bool]:
    """Toggle a source's enabled state."""
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    source.enabled = not source.enabled
    db.flush()

    log.info("api.source.toggled", source=source.name, enabled=source.enabled)
    return {"id": str(source.id), "name": source.name, "enabled": source.enabled}
