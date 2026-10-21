"""FastAPI web application — search, detail, and source routes."""

import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aoi.db.enums import OpportunityStatus
from aoi.db.models import (
    Fetch,
    FieldAssertion,
    Opportunity,
    OpportunityVersion,
    PipelineRun,
    Source,
    SourceDocument,
)
from aoi.pipeline.explain import explain
from aoi.web.deps import get_db, limiter

log = structlog.get_logger()

_WEB_DIR = Path(__file__).parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"

PAGE_SIZE = 20

# EU member states — ISO 3166-1 alpha-2
EU_COUNTRIES: dict[str, str] = {
    "AT": "Austria",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "HR": "Croatia",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DK": "Denmark",
    "EE": "Estonia",
    "FI": "Finland",
    "FR": "France",
    "DE": "Germany",
    "GR": "Greece",
    "HU": "Hungary",
    "IE": "Ireland",
    "IT": "Italy",
    "LV": "Latvia",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "MT": "Malta",
    "NL": "Netherlands",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "ES": "Spain",
    "SE": "Sweden",
}

# Non-EU country names for display
COUNTRY_NAMES: dict[str, str] = {
    "CH": "Switzerland",
    "GB": "United Kingdom",
    "US": "United States",
    "CA": "Canada",
    "AU": "Australia",
    "JP": "Japan",
    "CN": "China",
    "IN": "India",
    "BR": "Brazil",
    "NO": "Norway",
    "IL": "Israel",
    "KR": "South Korea",
    "SG": "Singapore",
    "NZ": "New Zealand",
    "TR": "Turkey",
    "IS": "Iceland",
    "RS": "Serbia",
    "UA": "Ukraine",
    "ZA": "South Africa",
    "MX": "Mexico",
}


def format_country(code: str | None) -> str:
    """Format a country code for display. EU members show as 'European Union (Name)'."""
    if code is None:
        return "Unknown"
    code = code.upper()
    if code in EU_COUNTRIES:
        return f"European Union ({EU_COUNTRIES[code]})"
    if code in COUNTRY_NAMES:
        return COUNTRY_NAMES[code]
    return code


def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    return Response(
        content="Rate limit exceeded. Please try again later.",
        status_code=429,
        media_type="text/plain",
    )


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    from aoi.config import get_settings

    get_settings().validate_production_secret()

    app = FastAPI(title="AOI — Academic Opportunity Index", docs_url=None, redoc_url=None)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)  # type: ignore[arg-type]

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.globals["format_country"] = format_country

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ------------------------------------------------------------------
    # Middleware
    # ------------------------------------------------------------------

    @app.middleware("http")
    async def add_csp_and_logging(request: Request, call_next: Any) -> Response:
        start = time.monotonic()
        response: Response = await call_next(request)
        elapsed_ms = round((time.monotonic() - start) * 1000)

        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self'; "
            "img-src 'self'; "
            "script-src 'none'; "
            "object-src 'none'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if "/static/" not in str(request.url.path):
            response.headers["Cache-Control"] = "no-store"

        log.info(
            "http.request",
            method=request.method,
            path=str(request.url.path),
            status=response.status_code,
            elapsed_ms=elapsed_ms,
            ip=request.client.host if request.client else "unknown",
            user_agent=request.headers.get("user-agent", ""),
        )
        return response

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, str]:
        """Lightweight health check for nginx/Docker probes (no auth required)."""
        return {"status": "ok"}

    @app.get("/")
    @limiter.limit("30/minute")
    def search(
        request: Request,
        q: str = Query(default="", max_length=200),
        country: str = Query(default="", max_length=2),
        source: str = Query(default="", max_length=200),
        page: int = Query(default=1, ge=1),
        db: Session = Depends(get_db),  # noqa: B008
    ) -> Response:
        """Full-text search with optional country and source filters."""
        offset = (page - 1) * PAGE_SIZE

        stmt = select(Opportunity).where(
            Opportunity.status != OpportunityStatus.CLOSED,
        )

        if q.strip():
            ts_query = func.plainto_tsquery("simple", q.strip())
            stmt = stmt.where(Opportunity.search_vector.op("@@")(ts_query))
            stmt = stmt.order_by(
                func.ts_rank(Opportunity.search_vector, ts_query).desc(),
            )
        else:
            stmt = stmt.order_by(Opportunity.last_seen_at.desc())

        if country.strip():
            stmt = stmt.where(Opportunity.country == country.strip().upper())

        if source.strip():
            # Filter via provenance chain: Opportunity → FieldAssertion → SourceDocument → Source
            source_subq = (
                select(FieldAssertion.opportunity_id)
                .join(SourceDocument, FieldAssertion.source_document_id == SourceDocument.id)
                .join(Source, SourceDocument.source_id == Source.id)
                .where(
                    Source.name == source.strip(),
                    FieldAssertion.is_current.is_(True),
                )
                .distinct()
                .subquery()
            )
            stmt = stmt.where(Opportunity.id.in_(select(source_subq)))

        # Total count for pagination
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total: int = db.execute(count_stmt).scalar() or 0
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

        results = db.execute(stmt.offset(offset).limit(PAGE_SIZE)).scalars().all()

        # Collect source names for each opportunity
        opp_sources: dict[uuid.UUID, list[str]] = {}
        if results:
            opp_ids = [r.id for r in results]
            source_rows = db.execute(
                select(FieldAssertion.opportunity_id, Source.name)
                .join(
                    SourceDocument,
                    FieldAssertion.source_document_id == SourceDocument.id,
                )
                .join(Source, SourceDocument.source_id == Source.id)
                .where(
                    FieldAssertion.opportunity_id.in_(opp_ids),
                    FieldAssertion.is_current.is_(True),
                )
                .distinct()
            ).all()
            for opp_id, src_name in source_rows:
                opp_sources.setdefault(opp_id, []).append(src_name)

        # Available sources and countries for filter dropdowns
        all_sources: list[str] = [
            row[0]
            for row in db.execute(
                select(Source.name).where(Source.enabled.is_(True)).order_by(Source.name)
            ).all()
        ]

        return templates.TemplateResponse(
            request,
            "search.html",
            {
                "results": results,
                "opp_sources": opp_sources,
                "q": q,
                "country": country,
                "source": source,
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "all_sources": all_sources,
            },
        )

    @app.get("/opportunity/{opp_id}")
    @limiter.limit("60/minute")
    def opportunity_detail(
        request: Request,
        opp_id: uuid.UUID,
        db: Session = Depends(get_db),  # noqa: B008
    ) -> Response:
        """Show opportunity detail with full provenance."""
        opp = db.get(Opportunity, opp_id)
        if opp is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")

        # Provenance report
        provenance = explain(db, opp_id)

        # Sources for this opportunity
        sources: list[Source] = list(
            db.execute(
                select(Source)
                .join(SourceDocument, Source.id == SourceDocument.source_id)
                .join(
                    FieldAssertion,
                    FieldAssertion.source_document_id == SourceDocument.id,
                )
                .where(
                    FieldAssertion.opportunity_id == opp_id,
                    FieldAssertion.is_current.is_(True),
                )
                .distinct()
            )
            .scalars()
            .all()
        )

        # Version history
        versions = (
            db.execute(
                select(OpportunityVersion)
                .where(OpportunityVersion.opportunity_id == opp_id)
                .order_by(OpportunityVersion.version_number.desc())
            )
            .scalars()
            .all()
        )

        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "opp": opp,
                "provenance": provenance,
                "sources": sources,
                "versions": versions,
            },
        )

    @app.get("/source/{source_id}")
    @limiter.limit("60/minute")
    def source_detail(
        request: Request,
        source_id: uuid.UUID,
        db: Session = Depends(get_db),  # noqa: B008
    ) -> Response:
        """Show source metadata and crawl stats."""
        src = db.get(Source, source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="Source not found")

        # Crawl stats
        total_fetches: int = (
            db.execute(select(func.count()).where(Fetch.source_id == source_id)).scalar() or 0
        )

        last_fetch_at = db.execute(
            select(func.max(Fetch.fetched_at)).where(Fetch.source_id == source_id)
        ).scalar()

        # Recent opportunities from this source (via provenance chain)
        recent_opps = (
            db.execute(
                select(Opportunity)
                .join(
                    FieldAssertion,
                    FieldAssertion.opportunity_id == Opportunity.id,
                )
                .join(
                    SourceDocument,
                    FieldAssertion.source_document_id == SourceDocument.id,
                )
                .where(
                    SourceDocument.source_id == source_id,
                    FieldAssertion.is_current.is_(True),
                )
                .distinct()
                .order_by(Opportunity.last_seen_at.desc())
                .limit(20)
            )
            .scalars()
            .all()
        )

        return templates.TemplateResponse(
            request,
            "source.html",
            {
                "source": src,
                "total_fetches": total_fetches,
                "last_fetch_at": last_fetch_at,
                "recent_opps": recent_opps,
            },
        )

    # Import and include dashboard router
    from aoi.web.dashboard import router as dashboard_router

    app.include_router(dashboard_router)

    # Import and include API router
    from aoi.web.api import router as api_router

    app.include_router(api_router)

    # ------------------------------------------------------------------
    # Control page (HTML)
    # ------------------------------------------------------------------

    @app.get("/control")
    @limiter.limit("30/minute")
    def control_page(
        request: Request,
        message: str = Query(default=""),
        message_type: str = Query(default="info"),
        db: Session = Depends(get_db),  # noqa: B008
    ) -> Response:
        """Pipeline control page — trigger runs, toggle sources."""
        sources = db.execute(select(Source).order_by(Source.name)).scalars().all()

        # Recent runs (last 20)
        runs = db.execute(
            select(PipelineRun, Source.name)
            .join(Source, PipelineRun.source_id == Source.id)
            .order_by(PipelineRun.started_at.desc())
            .limit(20)
        ).all()

        # Sources with a currently-running pipeline
        running_sources: set[str] = {
            src_name
            for _, src_name in db.execute(
                select(PipelineRun.id, Source.name)
                .join(Source, PipelineRun.source_id == Source.id)
                .where(PipelineRun.status == "running")
            ).all()
        }

        return templates.TemplateResponse(
            request,
            "control.html",
            {
                "sources": sources,
                "runs": runs,
                "running_sources": running_sources,
                "message": message,
                "message_type": message_type,
            },
        )

    @app.post("/control/run/{source_name}")
    @limiter.limit("5/minute")
    def control_trigger_run(
        request: Request,
        source_name: str,
        db: Session = Depends(get_db),  # noqa: B008
    ) -> Response:
        """Form POST handler: trigger a pipeline run, then redirect to control page."""
        source = db.query(Source).filter_by(name=source_name, enabled=True).first()
        if source is None:
            return _redirect_control("Source not found or not enabled", "error")

        # Check for already-running
        running = db.query(PipelineRun).filter_by(source_id=source.id, status="running").first()
        if running is not None:
            return _redirect_control(f"{source_name} already has a running pipeline", "error")

        run = PipelineRun(
            source_id=source.id,
            started_at=datetime.now(UTC),
            status="running",
            triggered_by="api",
        )
        db.add(run)
        db.flush()

        subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "aoi.pipeline.run",
                "--source",
                source_name,
                "--run-id",
                str(run.id),
            ],
            start_new_session=True,
        )

        log.info("control.run.triggered", source=source_name, run_id=str(run.id))
        return _redirect_control(f"Pipeline started for {source_name}", "success")

    @app.post("/control/toggle/{source_id}")
    @limiter.limit("10/minute")
    def control_toggle_source(
        request: Request,
        source_id: uuid.UUID,
        db: Session = Depends(get_db),  # noqa: B008
    ) -> Response:
        """Form POST handler: toggle source enabled state, redirect to control page."""
        source = db.get(Source, source_id)
        if source is None:
            return _redirect_control("Source not found", "error")

        source.enabled = not source.enabled
        db.flush()

        state = "enabled" if source.enabled else "disabled"
        log.info("control.source.toggled", source=source.name, enabled=source.enabled)
        return _redirect_control(f"{source.name} {state}", "success")

    def _redirect_control(message: str, message_type: str) -> Response:
        """Redirect to /control with a flash message via query params."""
        from urllib.parse import quote

        return Response(
            status_code=303,
            headers={"Location": f"/control?message={quote(message)}&message_type={message_type}"},
        )

    return app
