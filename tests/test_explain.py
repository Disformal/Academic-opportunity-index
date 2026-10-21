"""Tests for explain — aoi.pipeline.explain."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from aoi.db.enums import Derivation, IdentityKind, OpportunityStatus
from aoi.db.models import (
    CrawlUrl,
    ExtractionRun,
    Fetch,
    FieldAssertion,
    Opportunity,
    Source,
    SourceDocument,
)
from aoi.pipeline.canonicalize import url_hash
from aoi.pipeline.explain import explain

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


def _source(db: Session, name: str, trust_tier: int = 1) -> Source:
    src = Source(
        name=name,
        domain="example.com",
        endpoint_url="https://example.com/api",
        source_type="api",
        enabled=True,
        first_party=True,
        trust_tier=trust_tier,
    )
    db.add(src)
    db.flush()
    return src


def _full_chain(
    db: Session,
    source: Source,
    opp: Opportunity,
    *,
    field_path: str,
    value: object,
    raw_value: str | None = None,
    derivation: Derivation = Derivation.OFFICIAL_API,
    confidence: float = 0.95,
    json_pointer: str | None = "/test",
) -> FieldAssertion:
    """Create the full provenance chain ending in a FieldAssertion."""
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
        items_emitted=1,
    )
    db.add(erun)
    db.flush()

    fa = FieldAssertion(
        opportunity_id=opp.id,
        field_path=field_path,
        normalized_value=value,
        raw_value=raw_value or str(value),
        confidence=confidence,
        derivation=derivation,
        source_document_id=sdoc.id,
        extraction_run_id=erun.id,
        json_pointer=json_pointer,
        is_current=True,
    )
    db.add(fa)
    db.flush()
    return fa


def _opp(db: Session, identity_key: str) -> Opportunity:
    opp = Opportunity(
        identity_kind=IdentityKind.NATIVE_REF,
        identity_key=identity_key,
        title="Postdoc in QCD",
        type="postdoc",
        status=OpportunityStatus.OPEN,
        primary_language="en",
        first_seen_at=_NOW,
        last_seen_at=_NOW,
    )
    db.add(opp)
    db.flush()
    return opp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExplain:
    def test_returns_all_current_fields(self, db: Session) -> None:
        src = _source(db, "explain-test-1")
        opp = _opp(db, "exp-1")
        _full_chain(db, src, opp, field_path="title", value="Postdoc in QCD")
        _full_chain(db, src, opp, field_path="country", value="SE")
        _full_chain(db, src, opp, field_path="deadline", value="2027-01-31")

        result = explain(db, opp.id)
        assert set(result.keys()) == {"title", "country", "deadline"}

    def test_reports_winner_and_score(self, db: Session) -> None:
        src = _source(db, "explain-test-2")
        opp = _opp(db, "exp-2")
        _full_chain(db, src, opp, field_path="title", value="Postdoc in QCD")

        result = explain(db, opp.id)
        assert result["title"]["winner"] == "explain-test-2"
        assert result["title"]["score"] > 0
        assert result["title"]["value"] == "Postdoc in QCD"

    def test_competing_sources_listed(self, db: Session) -> None:
        src_a = _source(db, "explain-test-3a")
        src_b = _source(db, "explain-test-3b", trust_tier=3)
        opp = _opp(db, "exp-3")

        _full_chain(
            db,
            src_a,
            opp,
            field_path="deadline",
            value="2027-01-31",
            derivation=Derivation.OFFICIAL_API,
        )
        _full_chain(
            db,
            src_b,
            opp,
            field_path="deadline",
            value="2027-02-01",
            derivation=Derivation.PARSER,
            confidence=0.80,
        )

        result = explain(db, opp.id)
        assert result["deadline"]["winner"] == "explain-test-3a"
        assert len(result["deadline"]["competing"]) == 1
        assert result["deadline"]["competing"][0]["value"] == "2027-02-01"

    def test_excludes_superseded_assertions(self, db: Session) -> None:
        """Only is_current=True assertions should be included."""
        src = _source(db, "explain-test-4")
        opp = _opp(db, "exp-4")

        fa_old = _full_chain(db, src, opp, field_path="title", value="Old Title")
        fa_old.is_current = False
        db.flush()

        _full_chain(db, src, opp, field_path="title", value="New Title")

        result = explain(db, opp.id)
        assert result["title"]["value"] == "New Title"

    def test_empty_when_no_assertions(self, db: Session) -> None:
        opp = _opp(db, "exp-5")
        result = explain(db, opp.id)
        assert result == {}

    def test_grounding_info_included(self, db: Session) -> None:
        """Each field report should include derivation and grounding."""
        src = _source(db, "explain-test-6")
        opp = _opp(db, "exp-6")
        _full_chain(
            db,
            src,
            opp,
            field_path="title",
            value="Postdoc",
            derivation=Derivation.OFFICIAL_API,
            json_pointer="/metadata/position",
        )

        result = explain(db, opp.id)
        assert result["title"]["derivation"] == "official_api"
        assert result["title"]["json_pointer"] == "/metadata/position"
