"""Tests for the AOI dashboard route."""

import uuid
from datetime import UTC, datetime, timedelta

from starlette.testclient import TestClient

from aoi.db.enums import (
    FetchOutcome,
    IdentityKind,
    OpportunityStatus,
    PageRole,
    PublicationState,
)
from aoi.db.models import CrawlUrl, Fetch, Opportunity, Source
from aoi.web.app import create_app
from aoi.web.deps import get_db


def _make_app(db_session):  # type: ignore[no-untyped-def]
    app = create_app()
    app.state.limiter.enabled = False

    def _override():  # type: ignore[no-untyped-def]
        yield db_session

    app.dependency_overrides[get_db] = _override
    return app


def _make_source(db, *, name: str = "test-src", enabled: bool = True) -> Source:  # type: ignore[no-untyped-def]
    src = Source(
        id=uuid.uuid4(),
        name=name,
        domain=f"{name}.example.com",
        endpoint_url=f"https://{name}.example.com/api",
        source_type="api",
        enabled=enabled,
    )
    db.add(src)
    db.flush()
    return src


def _make_opportunity(  # type: ignore[no-untyped-def]
    db,
    *,
    title: str = "Test Opp",
    status: OpportunityStatus = OpportunityStatus.OPEN,
) -> Opportunity:
    now = datetime.now(UTC)
    opp = Opportunity(
        id=uuid.uuid4(),
        identity_kind=IdentityKind.FINGERPRINT,
        identity_key=uuid.uuid4().hex,
        title=title,
        type="postdoc",
        status=status,
        publication_state=PublicationState.UNVERIFIED,
        primary_language="en",
        country="DE",
        first_seen_at=now,
        last_seen_at=now,
        closed_at=now if status == OpportunityStatus.CLOSED else None,
    )
    db.add(opp)
    db.flush()
    return opp


def _make_fetch(
    db,  # type: ignore[no-untyped-def]
    source: Source,
    *,
    outcome: FetchOutcome = FetchOutcome.HIT,
    fetched_at: datetime | None = None,
) -> Fetch:
    if fetched_at is None:
        fetched_at = datetime.now(UTC)
    crawl_url = CrawlUrl(
        id=uuid.uuid4(),
        source_id=source.id,
        url_canonical=f"https://{source.domain}/page",
        url_original=f"https://{source.domain}/page",
        url_hash=uuid.uuid4().bytes[:32],
        page_role=PageRole.LISTING,
    )
    db.add(crawl_url)
    db.flush()
    fetch = Fetch(
        id=uuid.uuid4(),
        crawl_url_id=crawl_url.id,
        source_id=source.id,
        fetched_at=fetched_at,
        http_status=200 if outcome == FetchOutcome.HIT else 500,
        outcome=outcome,
    )
    db.add(fetch)
    db.flush()
    return fetch


class TestDashboard:
    def test_dashboard_renders(self, db) -> None:  # type: ignore[no-untyped-def]
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Pipeline Health Dashboard" in resp.text

    def test_dashboard_shows_aggregates(self, db) -> None:  # type: ignore[no-untyped-def]
        _make_opportunity(db, title="Open One")
        _make_opportunity(db, title="Open Two")
        _make_opportunity(db, title="Closed One", status=OpportunityStatus.CLOSED)

        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/dashboard")
        assert resp.status_code == 200
        # Should show total count somewhere in the page
        assert "Total Opportunities" in resp.text

    def test_dashboard_shows_source_health(self, db) -> None:  # type: ignore[no-untyped-def]
        src = _make_source(db, name="health-test-src")
        _make_fetch(db, src, outcome=FetchOutcome.HIT)

        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "health-test-src" in resp.text

    def test_dashboard_shows_fetch_outcomes(self, db) -> None:  # type: ignore[no-untyped-def]
        src = _make_source(db, name="fetch-outcome-src")
        _make_fetch(db, src, outcome=FetchOutcome.HIT)
        _make_fetch(db, src, outcome=FetchOutcome.ERROR)

        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "Fetch Outcomes" in resp.text

    def test_dashboard_consecutive_failures(self, db) -> None:  # type: ignore[no-untyped-def]
        src = _make_source(db, name="fail-src")
        now = datetime.now(UTC)
        # 3 consecutive errors (most recent first)
        _make_fetch(db, src, outcome=FetchOutcome.ERROR, fetched_at=now)
        _make_fetch(db, src, outcome=FetchOutcome.ERROR, fetched_at=now - timedelta(hours=1))
        _make_fetch(db, src, outcome=FetchOutcome.ERROR, fetched_at=now - timedelta(hours=2))
        _make_fetch(db, src, outcome=FetchOutcome.HIT, fetched_at=now - timedelta(hours=3))

        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "fail-src" in resp.text
