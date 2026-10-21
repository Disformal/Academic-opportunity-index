"""Tests for the AOI web application routes."""

import uuid
from datetime import UTC, datetime

from starlette.testclient import TestClient

from aoi.db.enums import IdentityKind, OpportunityStatus, PublicationState
from aoi.db.models import Opportunity, Source
from aoi.web.app import create_app
from aoi.web.deps import get_db


def _make_app(db_session):  # type: ignore[no-untyped-def]
    """Create app with test DB session override and rate limiting disabled."""
    app = create_app()
    app.state.limiter.enabled = False

    def _override():  # type: ignore[no-untyped-def]
        yield db_session

    app.dependency_overrides[get_db] = _override
    return app


def _make_opportunity(
    db,  # type: ignore[no-untyped-def]
    *,
    title: str = "Postdoc in Physics",
    opp_type: str = "postdoc",
    country: str = "DE",
    status: OpportunityStatus = OpportunityStatus.OPEN,
) -> Opportunity:
    """Insert a minimal Opportunity row for testing."""
    now = datetime.now(UTC)
    opp = Opportunity(
        id=uuid.uuid4(),
        identity_kind=IdentityKind.FINGERPRINT,
        identity_key=uuid.uuid4().hex,
        title=title,
        type=opp_type,
        status=status,
        publication_state=PublicationState.UNVERIFIED,
        primary_language="en",
        country=country,
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(opp)
    db.flush()
    return opp


def _make_source(db, *, name: str = "inspire", enabled: bool = True) -> Source:  # type: ignore[no-untyped-def]
    """Insert a minimal Source row for testing."""
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


class TestSearchRoute:
    def test_search_returns_html(self, db) -> None:  # type: ignore[no-untyped-def]
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_search_with_query_uses_tsvector(self, db) -> None:  # type: ignore[no-untyped-def]
        _make_opportunity(db, title="Quantum Computing Research Fellow")
        _make_opportunity(db, title="Marine Biology Assistant")
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/?q=quantum")
        assert resp.status_code == 200
        assert "Quantum Computing" in resp.text
        assert "Marine Biology" not in resp.text

    def test_search_filters_by_country(self, db) -> None:  # type: ignore[no-untyped-def]
        _make_opportunity(db, title="Job in Germany", country="DE")
        _make_opportunity(db, title="Job in France", country="FR")
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/?country=DE")
        assert resp.status_code == 200
        assert "Job in Germany" in resp.text
        assert "Job in France" not in resp.text

    def test_search_excludes_closed(self, db) -> None:  # type: ignore[no-untyped-def]
        _make_opportunity(db, title="Open Position")
        # Insert closed opportunity with closed_at set (ck_closed_consistent)
        now = datetime.now(UTC)
        closed_opp = Opportunity(
            id=uuid.uuid4(),
            identity_kind=IdentityKind.FINGERPRINT,
            identity_key=uuid.uuid4().hex,
            title="Closed Position",
            type="postdoc",
            status=OpportunityStatus.CLOSED,
            publication_state=PublicationState.UNVERIFIED,
            primary_language="en",
            country="DE",
            first_seen_at=now,
            last_seen_at=now,
            closed_at=now,
        )
        db.add(closed_opp)
        db.flush()

        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/")
        assert resp.status_code == 200
        assert "Open Position" in resp.text
        assert "Closed Position" not in resp.text

    def test_search_pagination(self, db) -> None:  # type: ignore[no-untyped-def]
        for i in range(25):
            _make_opportunity(db, title=f"Opportunity {i:03d}")
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp1 = client.get("/")
        assert resp1.status_code == 200
        assert "Page 1 of 2" in resp1.text

        resp2 = client.get("/?page=2")
        assert resp2.status_code == 200
        assert "Page 2 of 2" in resp2.text

    def test_search_shows_eu_country_format(self, db) -> None:  # type: ignore[no-untyped-def]
        _make_opportunity(db, title="Position in Germany", country="DE")
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/")
        assert resp.status_code == 200
        assert "European Union (Germany)" in resp.text


class TestDetailRoute:
    def test_detail_shows_opportunity(self, db) -> None:  # type: ignore[no-untyped-def]
        opp = _make_opportunity(db, title="Specific Research Position")
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(f"/opportunity/{opp.id}")
        assert resp.status_code == 200
        assert "Specific Research Position" in resp.text

    def test_detail_404_for_missing(self, db) -> None:  # type: ignore[no-untyped-def]
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(f"/opportunity/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_detail_shows_eu_country(self, db) -> None:  # type: ignore[no-untyped-def]
        opp = _make_opportunity(db, title="Position at CERN", country="CH")
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(f"/opportunity/{opp.id}")
        assert resp.status_code == 200
        # CH is not EU, should show "Switzerland"
        assert "Switzerland" in resp.text


class TestSourceRoute:
    def test_source_detail(self, db) -> None:  # type: ignore[no-untyped-def]
        src = _make_source(db, name="test-source")
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(f"/source/{src.id}")
        assert resp.status_code == 200
        assert "test-source" in resp.text

    def test_source_404_for_missing(self, db) -> None:  # type: ignore[no-untyped-def]
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(f"/source/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestHealthEndpoint:
    def test_health_returns_ok(self, db) -> None:  # type: ignore[no-untyped-def]
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestSecurityHeaders:
    def test_csp_header_present(self, db) -> None:  # type: ignore[no-untyped-def]
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/")
        assert "content-security-policy" in resp.headers
        csp = resp.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'none'" in csp

    def test_x_content_type_options(self, db) -> None:  # type: ignore[no-untyped-def]
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_x_frame_options(self, db) -> None:  # type: ignore[no-untyped-def]
        app = _make_app(db)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/")
        assert resp.headers.get("x-frame-options") == "DENY"
