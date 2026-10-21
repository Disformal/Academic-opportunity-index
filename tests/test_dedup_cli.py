"""Tests for dedup CLI functions — export_candidates and evaluate_pairs."""

import csv
import io
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from aoi.db.enums import IdentityKind, OpportunityStatus
from aoi.db.models import Opportunity, Source
from aoi.pipeline.dedup import evaluate_pairs, export_candidates_csv

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


def _source(db: Session, name: str) -> Source:
    src = Source(
        name=name,
        domain="example.com",
        endpoint_url="https://example.com/api",
        source_type="api",
        enabled=True,
    )
    db.add(src)
    db.flush()
    return src


def _opp(
    db: Session,
    identity_key: str,
    *,
    title: str = "Postdoc in QCD",
    application_url: str | None = None,
    deadline: datetime | None = None,
    country: str | None = None,
) -> Opportunity:
    opp = Opportunity(
        identity_kind=IdentityKind.NATIVE_REF,
        identity_key=identity_key,
        title=title,
        type="postdoc",
        status=OpportunityStatus.OPEN,
        primary_language="en",
        deadline=deadline,
        country=country,
        application_url=application_url,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
    )
    db.add(opp)
    db.flush()
    return opp


# ---------------------------------------------------------------------------
# export_candidates_csv
# ---------------------------------------------------------------------------


class TestExportCandidates:
    def test_export_produces_csv_with_header(self, db: Session) -> None:
        _source(db, "cli-src-1")
        _opp(
            db,
            "cli-1a",
            title="Postdoc in QCD",
            application_url="https://example.com/apply",
        )
        _opp(
            db,
            "cli-1b",
            title="Postdoc in QCD",
            application_url="https://example.com/apply",
        )

        buf = io.StringIO()
        export_candidates_csv(db, buf, limit=10)
        buf.seek(0)
        reader = csv.DictReader(buf)
        assert "id_a" in reader.fieldnames
        assert "label" in reader.fieldnames

    def test_export_finds_matching_pairs(self, db: Session) -> None:
        _source(db, "cli-src-2")
        _opp(
            db,
            "cli-2a",
            title="Postdoc in QCD",
            application_url="https://example.com/apply",
        )
        _opp(
            db,
            "cli-2b",
            title="Postdoc in QCD",
            application_url="https://example.com/apply",
        )

        buf = io.StringIO()
        export_candidates_csv(db, buf, limit=10)
        buf.seek(0)
        rows = list(csv.DictReader(buf))
        assert len(rows) >= 1

    def test_export_respects_limit(self, db: Session) -> None:
        _source(db, "cli-src-3")
        url = "https://example.com/shared"
        for i in range(5):
            _opp(db, f"cli-3-{i}", application_url=url)

        buf = io.StringIO()
        export_candidates_csv(db, buf, limit=2)
        buf.seek(0)
        rows = list(csv.DictReader(buf))
        assert len(rows) <= 2


# ---------------------------------------------------------------------------
# evaluate_pairs
# ---------------------------------------------------------------------------


class TestEvaluatePairs:
    def test_evaluate_computes_metrics(self) -> None:
        """Evaluate should return precision/recall at multiple thresholds."""
        csv_text = (
            "id_a,id_b,title_a,title_b,probability,classification,label\n"
            "a,b,T1,T2,0.98,auto_merge,same\n"
            "c,d,T3,T4,0.96,review,different\n"
            "e,f,T5,T6,0.91,review,same\n"
            "g,h,T7,T8,0.80,distinct,different\n"
        )
        buf = io.StringIO(csv_text)
        results = evaluate_pairs(buf)
        assert len(results) > 0
        assert all("threshold" in r for r in results)
        assert all("precision" in r for r in results)
        assert all("recall" in r for r in results)

    def test_evaluate_perfect_labels(self) -> None:
        """All same above threshold, all different below → precision 1.0."""
        csv_text = (
            "id_a,id_b,title_a,title_b,probability,classification,label\n"
            "a,b,T1,T2,0.99,auto_merge,same\n"
            "c,d,T3,T4,0.98,auto_merge,same\n"
            "e,f,T5,T6,0.50,distinct,different\n"
        )
        buf = io.StringIO(csv_text)
        results = evaluate_pairs(buf)
        # At threshold 0.97, both 0.99 and 0.98 are above → 2 true positives
        t97 = next(r for r in results if r["threshold"] == 0.97)
        assert t97["precision"] == 1.0

    def test_evaluate_skips_unlabelled_rows(self) -> None:
        csv_text = (
            "id_a,id_b,title_a,title_b,probability,classification,label\n"
            "a,b,T1,T2,0.99,auto_merge,same\n"
            "c,d,T3,T4,0.96,review,\n"
        )
        buf = io.StringIO(csv_text)
        results = evaluate_pairs(buf)
        # Should still work with only 1 labelled row
        assert len(results) > 0
