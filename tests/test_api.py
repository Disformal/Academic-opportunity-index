"""Tests for the pipeline control API and control page (Stage K)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from aoi.db.models import PipelineRun, Source
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


def _make_run(
    db,  # type: ignore[no-untyped-def]
    source: Source,
    *,
    status: str = "completed",
    triggered_by: str = "cli",
) -> PipelineRun:
    """Insert a PipelineRun row."""
    run = PipelineRun(
        source_id=source.id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC) if status != "running" else None,
        status=status,
        triggered_by=triggered_by,
    )
    db.add(run)
    db.flush()
    return run


# ---------------------------------------------------------------------------
# GET /api/sources
# ---------------------------------------------------------------------------


class TestListSources:
    @pytest.mark.integration
    def test_returns_sources(self, db) -> None:  # type: ignore[no-untyped-def]
        _make_source(db, name="inspire")
        _make_source(db, name="euraxess")
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.get("/api/sources")
        assert resp.status_code == 200

        data = resp.json()
        assert len(data) == 2
        names = {s["name"] for s in data}
        assert names == {"inspire", "euraxess"}

    @pytest.mark.integration
    def test_empty_sources(self, db) -> None:  # type: ignore[no-untyped-def]
        client = TestClient(_make_app(db), raise_server_exceptions=False)
        resp = client.get("/api/sources")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# POST /api/sources/{source_id}/toggle
# ---------------------------------------------------------------------------


class TestToggleSource:
    @pytest.mark.integration
    def test_toggle_disables_enabled_source(self, db) -> None:  # type: ignore[no-untyped-def]
        src = _make_source(db, name="inspire", enabled=True)
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.post(f"/api/sources/{src.id}/toggle")
        assert resp.status_code == 200

        data = resp.json()
        assert data["enabled"] is False

    @pytest.mark.integration
    def test_toggle_enables_disabled_source(self, db) -> None:  # type: ignore[no-untyped-def]
        src = _make_source(db, name="inspire", enabled=False)
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.post(f"/api/sources/{src.id}/toggle")
        assert resp.status_code == 200

        data = resp.json()
        assert data["enabled"] is True

    @pytest.mark.integration
    def test_toggle_nonexistent_source_returns_404(self, db) -> None:  # type: ignore[no-untyped-def]
        client = TestClient(_make_app(db), raise_server_exceptions=False)
        resp = client.post(f"/api/sources/{uuid.uuid4()}/toggle")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/runs
# ---------------------------------------------------------------------------


class TestListRuns:
    @pytest.mark.integration
    def test_returns_recent_runs(self, db) -> None:  # type: ignore[no-untyped-def]
        src = _make_source(db)
        _make_run(db, src, status="completed")
        _make_run(db, src, status="failed")
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.get("/api/runs")
        assert resp.status_code == 200

        data = resp.json()
        assert len(data) == 2

    @pytest.mark.integration
    def test_filter_by_source_name(self, db) -> None:  # type: ignore[no-untyped-def]
        s1 = _make_source(db, name="inspire")
        s2 = _make_source(db, name="euraxess")
        _make_run(db, s1)
        _make_run(db, s2)
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.get("/api/runs?source_name=inspire")
        assert resp.status_code == 200

        data = resp.json()
        assert len(data) == 1
        assert data[0]["source"] == "inspire"

    @pytest.mark.integration
    def test_empty_runs(self, db) -> None:  # type: ignore[no-untyped-def]
        client = TestClient(_make_app(db), raise_server_exceptions=False)
        resp = client.get("/api/runs")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}
# ---------------------------------------------------------------------------


class TestGetRun:
    @pytest.mark.integration
    def test_returns_run_details(self, db) -> None:  # type: ignore[no-untyped-def]
        src = _make_source(db)
        run = _make_run(db, src, status="completed", triggered_by="api")
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.get(f"/api/runs/{run.id}")
        assert resp.status_code == 200

        data = resp.json()
        assert data["run_id"] == str(run.id)
        assert data["status"] == "completed"
        assert data["triggered_by"] == "api"

    @pytest.mark.integration
    def test_nonexistent_run_returns_404(self, db) -> None:  # type: ignore[no-untyped-def]
        client = TestClient(_make_app(db), raise_server_exceptions=False)
        resp = client.get(f"/api/runs/{uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/runs (trigger)
# ---------------------------------------------------------------------------


class TestTriggerRun:
    @pytest.mark.integration
    def test_trigger_creates_run_and_spawns_subprocess(self, db) -> None:  # type: ignore[no-untyped-def]
        _make_source(db, name="inspire")
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        with patch("aoi.web.api.subprocess.Popen") as mock_popen:
            resp = client.post("/api/runs?source_name=inspire")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert "run_id" in data
        mock_popen.assert_called_once()

    @pytest.mark.integration
    def test_trigger_unknown_source_returns_404(self, db) -> None:  # type: ignore[no-untyped-def]
        client = TestClient(_make_app(db), raise_server_exceptions=False)
        resp = client.post("/api/runs?source_name=nonexistent")
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_trigger_disabled_source_returns_404(self, db) -> None:  # type: ignore[no-untyped-def]
        _make_source(db, name="inspire", enabled=False)
        client = TestClient(_make_app(db), raise_server_exceptions=False)
        resp = client.post("/api/runs?source_name=inspire")
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_trigger_already_running_returns_409(self, db) -> None:  # type: ignore[no-untyped-def]
        src = _make_source(db, name="inspire")
        _make_run(db, src, status="running")
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.post("/api/runs?source_name=inspire")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# GET /control
# ---------------------------------------------------------------------------


class TestControlPage:
    @pytest.mark.integration
    def test_control_page_returns_html(self, db) -> None:  # type: ignore[no-untyped-def]
        _make_source(db, name="inspire")
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.get("/control")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Pipeline Control" in resp.text

    @pytest.mark.integration
    def test_control_page_shows_sources(self, db) -> None:  # type: ignore[no-untyped-def]
        _make_source(db, name="inspire")
        _make_source(db, name="euraxess")
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.get("/control")
        assert "inspire" in resp.text
        assert "euraxess" in resp.text

    @pytest.mark.integration
    def test_control_page_shows_runs(self, db) -> None:  # type: ignore[no-untyped-def]
        src = _make_source(db, name="inspire")
        _make_run(db, src, status="completed")
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.get("/control")
        assert "completed" in resp.text

    @pytest.mark.integration
    def test_control_page_shows_flash_message(self, db) -> None:  # type: ignore[no-untyped-def]
        client = TestClient(_make_app(db), raise_server_exceptions=False)
        resp = client.get("/control?message=Test+message&message_type=success")
        assert resp.status_code == 200
        assert "Test message" in resp.text


# ---------------------------------------------------------------------------
# POST /control/toggle/{source_id}
# ---------------------------------------------------------------------------


class TestControlToggle:
    @pytest.mark.integration
    def test_toggle_redirects_to_control(self, db) -> None:  # type: ignore[no-untyped-def]
        src = _make_source(db, name="inspire")
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.post(f"/control/toggle/{src.id}", follow_redirects=False)
        assert resp.status_code == 303
        assert "/control?" in resp.headers["location"]


# ---------------------------------------------------------------------------
# POST /control/run/{source_name}
# ---------------------------------------------------------------------------


class TestControlRun:
    @pytest.mark.integration
    def test_run_redirects_to_control(self, db) -> None:  # type: ignore[no-untyped-def]
        _make_source(db, name="inspire")
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        with patch("aoi.web.app.subprocess.Popen"):
            resp = client.post("/control/run/inspire", follow_redirects=False)

        assert resp.status_code == 303
        assert "Pipeline" in resp.headers["location"]
        assert "message_type=success" in resp.headers["location"]

    @pytest.mark.integration
    def test_run_nonexistent_source_redirects_with_error(self, db) -> None:  # type: ignore[no-untyped-def]
        client = TestClient(_make_app(db), raise_server_exceptions=False)

        resp = client.post("/control/run/nonexistent", follow_redirects=False)
        assert resp.status_code == 303
        assert "message_type=error" in resp.headers["location"]
