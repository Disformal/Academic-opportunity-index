"""Tests for pipeline orchestration — aoi.pipeline.run. Uses real DB."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from aoi.db.enums import (
    Derivation,
    IdentityKind,
    OpportunityStatus,
)
from aoi.db.models import (
    ExtractionRun,
    FieldAssertion,
    Opportunity,
    OpportunityVersion,
    Source,
)
from aoi.pipeline.run import (
    normalize_job_type,
    process_euraxess_item,
    process_inspire_item,
    run_source,
)
from aoi.pipeline.sources.euraxess import RawOpportunity
from aoi.pipeline.sources.inspire import RawJob

INSPIRE_FIXTURE = Path(__file__).parent / "fixtures" / "inspire_page.json"
EURAXESS_DETAIL_FIXTURE = Path(__file__).parent / "fixtures" / "euraxess_detail.html"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(db: Session, name: str, source_type: str = "api") -> Source:
    src = Source(
        name=name,
        domain="example.com",
        endpoint_url="https://example.com/api",
        source_type=source_type,
        enabled=True,
    )
    db.add(src)
    db.flush()
    return src


def _make_inspire_raw_job() -> RawJob:
    """Build a RawJob from the INSPIRE fixture."""
    data = json.loads(INSPIRE_FIXTURE.read_bytes())
    hit = data["hits"]["hits"][0]
    metadata = hit["metadata"]
    return RawJob(
        control_number=str(metadata["control_number"]),
        payload=metadata,
        fetch_id=uuid.uuid4(),
        content_digest="a" * 64,
        document_text=json.dumps(hit, ensure_ascii=False),
        pointers={
            "position": "/metadata/position",
            "deadline_date": "/metadata/deadline_date",
            "status": "/metadata/status",
            "institutions": "/metadata/institutions",
            "regions": "/metadata/regions",
            "ranks": "/metadata/ranks",
        },
    )


def _make_euraxess_raw_opportunity() -> RawOpportunity:
    """Build a RawOpportunity from the EURAXESS fixture."""
    return RawOpportunity(
        job_id="468285",
        title="Postdoctoral Researcher in Quantum Computing",
        fields={
            "Organisation": "ETH Zurich",
            "Research Field": "Physics",
            "Researcher Profile": "Recognised Researcher (R2)",
            "Country": "Switzerland",
            "Type of Contract": "Temporary",
        },
        description="The Department of Physics at ETH Zurich invites applications.",
        application_url="https://ethz.ch/en/jobs/postdoc-12345.html",
        deadline_iso="2026-10-26T22:59:59+00:00",
        detail_url="https://euraxess.ec.europa.eu/jobs/468285",
        listing_url="https://euraxess.ec.europa.eu/jobs/search?page=0",
        fetch_id=uuid.uuid4(),
        content_digest="b" * 64,
        document_text="Postdoctoral Researcher in Quantum Computing ETH Zurich Physics",
        field_positions={
            "Organisation": (44, 54),
        },
    )


# ---------------------------------------------------------------------------
# normalize_job_type
# ---------------------------------------------------------------------------


class TestNormalizeJobType:
    def test_exact_match(self) -> None:
        assert normalize_job_type("POSTDOC") == "postdoc"

    def test_case_insensitive(self) -> None:
        assert normalize_job_type("phd") == "phd"
        assert normalize_job_type("PhD") == "phd"

    def test_euraxess_researcher_profile(self) -> None:
        assert normalize_job_type("Recognised Researcher (R2)") == "postdoc"
        assert normalize_job_type("First Stage Researcher (R1)") == "phd"
        assert normalize_job_type("Established Researcher (R3)") == "senior"
        assert normalize_job_type("Leading Researcher (R4)") == "faculty"

    def test_unknown_returns_other(self) -> None:
        assert normalize_job_type("Something Completely Unknown") == "other"

    def test_empty_string_returns_other(self) -> None:
        assert normalize_job_type("") == "other"


# ---------------------------------------------------------------------------
# process_inspire_item (integration — real DB)
# ---------------------------------------------------------------------------


class TestProcessInspireItem:
    def test_creates_opportunity_with_identity(self, db: Session) -> None:
        src = _make_source(db, "INSPIRE-test-1")
        raw = _make_inspire_raw_job()

        opp = process_inspire_item(db, src, raw)

        assert opp.identity_kind == IdentityKind.NATIVE_REF
        assert opp.identity_key == raw.control_number
        assert opp.title == "Postdoctoral Researcher"

    def test_creates_field_assertions_with_grounding(self, db: Session) -> None:
        src = _make_source(db, "INSPIRE-test-2")
        raw = _make_inspire_raw_job()

        opp = process_inspire_item(db, src, raw)

        assertions = (
            db.query(FieldAssertion).filter_by(opportunity_id=opp.id, is_current=True).all()
        )
        field_paths = {a.field_path for a in assertions}

        # Must have at least these 5 fields
        assert "title" in field_paths
        assert "type" in field_paths
        assert "deadline" in field_paths
        assert "status" in field_paths
        assert "country" in field_paths

        # All assertions must be grounded
        for a in assertions:
            if a.derivation not in (Derivation.HUMAN, Derivation.INHERITED):
                assert a.json_pointer is not None or a.text_start is not None

    def test_creates_extraction_run(self, db: Session) -> None:
        src = _make_source(db, "INSPIRE-test-3")
        raw = _make_inspire_raw_job()

        process_inspire_item(db, src, raw)

        erun = db.query(ExtractionRun).filter_by(extractor_id="inspire").first()
        assert erun is not None
        assert erun.outcome == "success"
        assert erun.items_emitted > 0

    def test_creates_version(self, db: Session) -> None:
        src = _make_source(db, "INSPIRE-test-4")
        raw = _make_inspire_raw_job()

        opp = process_inspire_item(db, src, raw)

        versions = db.query(OpportunityVersion).filter_by(opportunity_id=opp.id).all()
        assert len(versions) == 1
        assert versions[0].version_number == 1

    def test_idempotent_rerun_no_new_version(self, db: Session) -> None:
        src = _make_source(db, "INSPIRE-test-5")
        raw = _make_inspire_raw_job()

        opp1 = process_inspire_item(db, src, raw)
        opp2 = process_inspire_item(db, src, raw)

        assert opp1.id == opp2.id
        versions = db.query(OpportunityVersion).filter_by(opportunity_id=opp1.id).all()
        assert len(versions) == 1


# ---------------------------------------------------------------------------
# process_euraxess_item (integration — real DB)
# ---------------------------------------------------------------------------


class TestProcessEuraxessItem:
    def test_creates_opportunity_with_identity(self, db: Session) -> None:
        src = _make_source(db, "EURAXESS-test-1", source_type="html-listing")
        raw = _make_euraxess_raw_opportunity()

        opp = process_euraxess_item(db, src, raw)

        assert opp.identity_kind == IdentityKind.NATIVE_REF
        assert opp.identity_key == "468285"

    def test_creates_field_assertions(self, db: Session) -> None:
        src = _make_source(db, "EURAXESS-test-2", source_type="html-listing")
        raw = _make_euraxess_raw_opportunity()

        opp = process_euraxess_item(db, src, raw)

        assertions = (
            db.query(FieldAssertion).filter_by(opportunity_id=opp.id, is_current=True).all()
        )
        field_paths = {a.field_path for a in assertions}

        assert "title" in field_paths
        assert "type" in field_paths
        assert "deadline" in field_paths
        assert "application_url" in field_paths
        assert "organization" in field_paths

    def test_normalizes_type_from_researcher_profile(self, db: Session) -> None:
        src = _make_source(db, "EURAXESS-test-3", source_type="html-listing")
        raw = _make_euraxess_raw_opportunity()

        opp = process_euraxess_item(db, src, raw)

        assert opp.type == "postdoc"

    def test_creates_version(self, db: Session) -> None:
        src = _make_source(db, "EURAXESS-test-4", source_type="html-listing")
        raw = _make_euraxess_raw_opportunity()

        opp = process_euraxess_item(db, src, raw)

        versions = db.query(OpportunityVersion).filter_by(opportunity_id=opp.id).all()
        assert len(versions) == 1


# ---------------------------------------------------------------------------
# run_source (integration — mocked harvest, real DB + S3)
# ---------------------------------------------------------------------------


class TestRunSource:
    def test_inspire_run_creates_opportunities(self, db: Session) -> None:
        _make_source(db, "inspire")
        raw = _make_inspire_raw_job()

        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([raw])):
            result = run_source(db, "inspire")

        assert result.created > 0
        assert result.errors == 0

    def test_euraxess_run_creates_opportunities(self, db: Session) -> None:
        _make_source(db, "euraxess", source_type="html-listing")
        raw = _make_euraxess_raw_opportunity()

        with patch("aoi.pipeline.run._harvest_euraxess", return_value=iter([raw])):
            result = run_source(db, "euraxess")

        assert result.created > 0
        assert result.errors == 0

    def test_unknown_source_raises(self, db: Session) -> None:
        with pytest.raises(ValueError, match="Unknown source"):
            run_source(db, "unknown_source")


# ---------------------------------------------------------------------------
# Closure pass (integration — real DB)
# ---------------------------------------------------------------------------


class TestClosurePass:
    def test_deadline_passed_opportunity_gets_closed(self, db: Session) -> None:
        """An opportunity with a deadline >24h ago should be closed after a run."""
        _make_source(db, "inspire")
        raw = _make_inspire_raw_job()

        # First run: create the opportunity
        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([raw])):
            result = run_source(db, "inspire")
        assert result.created == 1

        # Set deadline to 48h in the past (simulating time passing)
        opp = db.query(Opportunity).filter_by(identity_kind=IdentityKind.NATIVE_REF).first()
        assert opp is not None
        opp.deadline = datetime.now(UTC) - timedelta(hours=48)
        opp.last_seen_at = datetime.now(UTC) - timedelta(hours=48)
        db.flush()

        # Second run: empty harvest → closure pass should close it
        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([])):
            result = run_source(db, "inspire")

        db.refresh(opp)
        assert opp.status == OpportunityStatus.CLOSED
        assert opp.closed_at is not None
        assert result.closed > 0

    def test_seen_opportunity_not_closed(self, db: Session) -> None:
        """An opportunity seen in the current run should not be closed."""
        _make_source(db, "inspire")
        raw = _make_inspire_raw_job()

        # First run
        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([raw])):
            run_source(db, "inspire")

        opp = db.query(Opportunity).filter_by(identity_kind=IdentityKind.NATIVE_REF).first()
        assert opp is not None
        opp.deadline = datetime.now(UTC) - timedelta(hours=48)
        db.flush()

        # Second run: same item is yielded again → should NOT be closed
        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([raw])):
            result = run_source(db, "inspire")

        db.refresh(opp)
        assert opp.status != OpportunityStatus.CLOSED
        assert result.closed == 0

    def test_circuit_breaker_blocks_mass_closure(self, db: Session) -> None:
        """Circuit breaker should prevent closing too many opportunities at once."""
        _make_source(db, "inspire")
        raw = _make_inspire_raw_job()

        # Create one opportunity
        with patch("aoi.pipeline.run._harvest_inspire", return_value=iter([raw])):
            run_source(db, "inspire")

        opp = db.query(Opportunity).filter_by(identity_kind=IdentityKind.NATIVE_REF).first()
        assert opp is not None
        opp.deadline = datetime.now(UTC) - timedelta(hours=48)
        opp.last_seen_at = datetime.now(UTC) - timedelta(hours=48)
        db.flush()

        # With a breaker that blocks everything (floor=0, proportion=0)
        with (
            patch("aoi.pipeline.run._harvest_inspire", return_value=iter([])),
            patch(
                "aoi.pipeline.circuit_breaker._policy",
                return_value={"proportion": 0, "floor": 0},
            ),
        ):
            result = run_source(db, "inspire")

        db.refresh(opp)
        assert opp.status != OpportunityStatus.CLOSED
        assert result.closed == 0
        assert result.quarantined > 0
