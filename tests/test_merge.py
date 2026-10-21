"""Tests for merge/unmerge — aoi.pipeline.dedup."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from aoi.db.enums import Derivation, IdentityKind, OpportunityStatus, PublicationState
from aoi.db.models import (
    CrawlUrl,
    DuplicateCluster,
    ExtractionRun,
    Fetch,
    FieldAssertion,
    Opportunity,
    Source,
    SourceDocument,
)
from aoi.pipeline.canonicalize import url_hash
from aoi.pipeline.dedup import merge, unmerge

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


def _opp(db: Session, identity_key: str, title: str = "Postdoc in QCD") -> Opportunity:
    """Create an opportunity with default UNVERIFIED state."""
    opp = Opportunity(
        identity_kind=IdentityKind.NATIVE_REF,
        identity_key=identity_key,
        title=title,
        type="postdoc",
        status=OpportunityStatus.OPEN,
        primary_language="en",
        first_seen_at=_NOW,
        last_seen_at=_NOW,
    )
    db.add(opp)
    db.flush()
    return opp


def _add_provenance(db: Session, source: Source, opp: Opportunity) -> None:
    """Add field assertions for title, deadline, application_url.

    Required by the provenance trigger before publication_state can be VERIFIED.
    """
    url = f"https://example.com/{uuid.uuid4()}"
    cu = CrawlUrl(
        source_id=source.id,
        url_canonical=url,
        url_original=url,
        url_hash=url_hash(url),
        page_role="detail",
        last_fetched_at=_NOW,
    )
    db.add(cu)
    db.flush()

    fetch = Fetch(
        crawl_url_id=cu.id,
        source_id=source.id,
        fetched_at=_NOW,
        http_status=200,
        outcome="hit",
        content_digest="a" * 64,
    )
    db.add(fetch)
    db.flush()

    sdoc = SourceDocument(
        fetch_id=fetch.id,
        source_id=source.id,
        content_digest="a" * 64,
        media_type="application/json",
        detected_language="en",
        char_count=100,
    )
    db.add(sdoc)
    db.flush()

    erun = ExtractionRun(
        source_document_id=sdoc.id,
        extractor_id="test",
        extractor_version="0.1.0",
        started_at=_NOW,
        finished_at=_NOW,
        outcome="success",
        items_emitted=3,
    )
    db.add(erun)
    db.flush()

    for field, value in [
        ("title", opp.title),
        ("deadline", "2027-01-31"),
        ("application_url", "https://example.com/apply"),
    ]:
        fa = FieldAssertion(
            opportunity_id=opp.id,
            field_path=field,
            normalized_value=value,
            raw_value=str(value),
            confidence=0.95,
            derivation=Derivation.OFFICIAL_API,
            source_document_id=sdoc.id,
            extraction_run_id=erun.id,
            is_current=True,
        )
        db.add(fa)
    db.flush()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMerge:
    def test_merge_creates_cluster_with_two_members(self, db: Session) -> None:
        a = _opp(db, "merge-1a")
        b = _opp(db, "merge-1b")
        cluster = merge(db, a, b, score=0.98)
        assert len(cluster.members) == 2

    def test_merge_sets_canonical_to_first_argument(self, db: Session) -> None:
        a = _opp(db, "merge-2a")
        b = _opp(db, "merge-2b")
        cluster = merge(db, a, b, score=0.98)
        assert cluster.canonical_opportunity_id == a.id

    def test_merge_both_rows_survive(self, db: Session) -> None:
        """Merging must never delete either record."""
        a = _opp(db, "merge-3a")
        b = _opp(db, "merge-3b")
        a_id, b_id = a.id, b.id
        merge(db, a, b, score=0.98)
        assert db.get(Opportunity, a_id) is not None
        assert db.get(Opportunity, b_id) is not None

    def test_merge_records_method_and_score(self, db: Session) -> None:
        a = _opp(db, "merge-4a")
        b = _opp(db, "merge-4b")
        cluster = merge(db, a, b, score=0.975, method="auto")
        assert cluster.method == "auto"
        assert float(cluster.score) == pytest.approx(0.975, abs=1e-3)

    def test_merge_stores_signals_on_members(self, db: Session) -> None:
        a = _opp(db, "merge-5a")
        b = _opp(db, "merge-5b")
        signals = {"title": 2.5, "application_url": 4.0}
        cluster = merge(db, a, b, score=0.98, signals=signals)
        member_signals = [m.signals for m in cluster.members if m.signals]
        assert len(member_signals) >= 1
        assert member_signals[0]["title"] == 2.5


class TestUnmerge:
    def test_merge_is_reversible(self, db: Session) -> None:
        """Build-manual spec: merge then unmerge restores both to VERIFIED."""
        src = _source(db, "unmerge-src-1")
        a = _opp(db, "unmerge-1a")
        b = _opp(db, "unmerge-1b")
        _add_provenance(db, src, a)
        _add_provenance(db, src, b)

        cluster = merge(db, a, b, score=0.98)
        unmerge(db, cluster.id)

        a_reloaded = db.get(Opportunity, a.id)
        b_reloaded = db.get(Opportunity, b.id)
        assert a_reloaded is not None
        assert b_reloaded is not None
        assert a_reloaded.publication_state == PublicationState.VERIFIED
        assert b_reloaded.publication_state == PublicationState.VERIFIED

    def test_unmerge_deletes_cluster(self, db: Session) -> None:
        src = _source(db, "unmerge-src-2")
        a = _opp(db, "unmerge-2a")
        b = _opp(db, "unmerge-2b")
        _add_provenance(db, src, a)
        _add_provenance(db, src, b)

        cluster = merge(db, a, b, score=0.98)
        cluster_id = cluster.id
        unmerge(db, cluster_id)
        assert db.get(DuplicateCluster, cluster_id) is None

    def test_unmerge_nonexistent_cluster_raises(self, db: Session) -> None:
        with pytest.raises(ValueError, match="not found"):
            unmerge(db, uuid.uuid4())
