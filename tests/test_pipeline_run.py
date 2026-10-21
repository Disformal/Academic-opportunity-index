"""Tests for pipeline orchestration (pipeline/run.py) — incremental harvesting and run history."""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from aoi.db.models import PipelineRun, Source
from aoi.pipeline.run import run_source


def _create_source(db: Session, name: str = "inspire") -> Source:
    """Insert a test source row."""
    source = Source(
        name=name,
        domain="inspirehep.net" if name == "inspire" else "euraxess.ec.europa.eu",
        endpoint_url="https://example.com/api",
        source_type="api" if name == "inspire" else "scraper",
        first_party=False,
        trust_tier=2,
        countries=[],
        languages=["en"],
        acquisition_decision="permitted",
        requests_per_second=2.0,
        enabled=True,
    )
    db.add(source)
    db.flush()
    return source


@pytest.mark.integration
def test_run_source_updates_last_harvested_at(db: Session) -> None:
    """After a pipeline run, source.last_harvested_at is set."""
    source = _create_source(db, "inspire")
    assert source.last_harvested_at is None

    # Mock the harvest to yield nothing (empty run)
    with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([])):
        run_source(db, "inspire")

    db.refresh(source)
    assert source.last_harvested_at is not None


@pytest.mark.integration
def test_run_source_known_keys_query_uses_provenance_chain(db: Session) -> None:
    """_harvest_inspire receives known_keys built from the provenance chain."""
    _create_source(db, "inspire")

    captured_known_keys: list[frozenset[str] | None] = []

    def mock_harvest(
        src: Source,
        session: Session,
        early_stop_consecutive: int = 10,
    ):  # type: ignore[no-untyped-def]
        # Capture the known_keys that would be queried
        from aoi.db.models import FieldAssertion, Opportunity, SourceDocument

        known = frozenset(
            row[0]
            for row in session.query(Opportunity.identity_key)
            .join(FieldAssertion, FieldAssertion.opportunity_id == Opportunity.id)
            .join(SourceDocument, FieldAssertion.source_document_id == SourceDocument.id)
            .where(SourceDocument.source_id == src.id)
            .distinct()
            .all()
        )
        captured_known_keys.append(known)
        return iter([])

    with patch("aoi.pipeline.run._harvest_inspire", side_effect=mock_harvest):
        run_source(db, "inspire")

    # With no existing opportunities, known_keys should be empty
    assert len(captured_known_keys) == 1
    assert captured_known_keys[0] == frozenset()


# ---------------------------------------------------------------------------
# Stage I — PipelineRun history persistence
# ---------------------------------------------------------------------------


class TestPipelineRunCreation:
    @pytest.mark.integration
    def test_run_source_creates_pipeline_run_row(self, db: Session) -> None:
        src = _create_source(db, "inspire")

        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([])):
            run_source(db, "inspire")

        runs = db.query(PipelineRun).filter_by(source_id=src.id).all()
        assert len(runs) == 1

    @pytest.mark.integration
    def test_completed_run_has_correct_status(self, db: Session) -> None:
        _create_source(db, "inspire")

        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([])):
            run_source(db, "inspire")

        run = db.query(PipelineRun).one()
        assert run.status == "completed"
        assert run.finished_at is not None
        assert run.finished_at >= run.started_at

    @pytest.mark.integration
    def test_run_records_triggered_by(self, db: Session) -> None:
        _create_source(db, "inspire")

        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([])):
            run_source(db, "inspire", triggered_by="api")

        run = db.query(PipelineRun).one()
        assert run.triggered_by == "api"

    @pytest.mark.integration
    def test_run_records_zero_counts_for_empty_harvest(self, db: Session) -> None:
        _create_source(db, "inspire")

        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([])):
            run_source(db, "inspire")

        run = db.query(PipelineRun).one()
        assert run.created == 0
        assert run.updated == 0
        assert run.unchanged == 0
        assert run.errors == 0
        assert run.closed == 0
        assert run.quarantined == 0

    @pytest.mark.integration
    def test_default_triggered_by_is_cli(self, db: Session) -> None:
        _create_source(db, "inspire")

        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([])):
            run_source(db, "inspire")

        run = db.query(PipelineRun).one()
        assert run.triggered_by == "cli"


class TestPipelineRunExistingRow:
    @pytest.mark.integration
    def test_uses_existing_run_id(self, db: Session) -> None:
        from datetime import UTC, datetime

        src = _create_source(db, "inspire")
        existing = PipelineRun(
            source_id=src.id,
            started_at=datetime.now(UTC),
            status="running",
            triggered_by="api",
        )
        db.add(existing)
        db.flush()
        run_id = existing.id

        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([])):
            run_source(db, "inspire", run_id=run_id)

        runs = db.query(PipelineRun).filter_by(source_id=src.id).all()
        assert len(runs) == 1
        assert runs[0].id == run_id
        assert runs[0].status == "completed"

    @pytest.mark.integration
    def test_raises_on_missing_run_id(self, db: Session) -> None:
        _create_source(db, "inspire")

        with pytest.raises(ValueError, match="not found"):
            run_source(db, "inspire", run_id=uuid.uuid4())


class TestPipelineRunFailure:
    @pytest.mark.integration
    def test_failed_run_records_error(self, db: Session) -> None:
        src = _create_source(db, "inspire")

        def _exploding_harvest(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("adapter crashed")

        with (
            patch("aoi.pipeline.run._harvest_inspire", side_effect=_exploding_harvest),
            pytest.raises(RuntimeError, match="adapter crashed"),
        ):
            run_source(db, "inspire")

        run = db.query(PipelineRun).filter_by(source_id=src.id).one()
        assert run.status == "failed"
        assert run.finished_at is not None
        assert "adapter crashed" in run.error_message  # type: ignore[operator]


class TestPipelineRunMultiple:
    @pytest.mark.integration
    def test_second_run_creates_new_row(self, db: Session) -> None:
        src = _create_source(db, "inspire")

        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([])):
            run_source(db, "inspire")
            run_source(db, "inspire")

        runs = db.query(PipelineRun).filter_by(source_id=src.id).all()
        assert len(runs) == 2
        assert runs[0].id != runs[1].id
