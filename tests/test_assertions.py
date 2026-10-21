"""Tests for provenance assertion writer (assertions.py). Uses real DB."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from aoi.db.enums import (
    Derivation,
    FetchOutcome,
    IdentityKind,
    OpportunityStatus,
    PageRole,
    PublicationState,
)
from aoi.db.models import (
    CrawlUrl,
    ExtractionRun,
    Fetch,
    FieldAssertion,
    Opportunity,
    Source,
    SourceDocument,
)
from aoi.pipeline.assertions import UngroundedAssertion, assert_field

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(db: Session, name: str = "Test Source") -> Source:
    src = Source(
        name=name,
        domain="example.com",
        endpoint_url="https://example.com/api",
        source_type="api",
    )
    db.add(src)
    db.flush()
    return src


def _make_opportunity(db: Session, key: str = "op-key") -> Opportunity:
    now = datetime.now(UTC)
    opp = Opportunity(
        identity_kind=IdentityKind.NATIVE_REF,
        identity_key=key,
        title="Test Opportunity",
        type="fellowship",
        status=OpportunityStatus.OPEN,
        publication_state=PublicationState.UNVERIFIED,
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(opp)
    db.flush()
    return opp


def _make_extraction_context(db: Session, source: Source) -> tuple[SourceDocument, ExtractionRun]:
    crawl_url = CrawlUrl(
        source_id=source.id,
        url_canonical="https://example.com/job/1",
        url_original="https://example.com/job/1",
        url_hash=b"\x00" * 32,
        page_role=PageRole.DETAIL,
    )
    db.add(crawl_url)
    db.flush()

    fetch = Fetch(
        crawl_url_id=crawl_url.id,
        source_id=source.id,
        fetched_at=datetime.now(UTC),
        outcome=FetchOutcome.HIT,
    )
    db.add(fetch)
    db.flush()

    sdoc = SourceDocument(
        fetch_id=fetch.id,
        source_id=source.id,
        content_digest="b" * 64,
        media_type="application/json",
    )
    db.add(sdoc)
    db.flush()

    erun = ExtractionRun(
        source_document_id=sdoc.id,
        extractor_id="inspire",
        extractor_version="0.1.0",
        started_at=datetime.now(UTC),
    )
    db.add(erun)
    db.flush()

    return sdoc, erun


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_assert_field_creates_field_assertion_with_is_current_true(db: Session) -> None:
    src = _make_source(db, "Source A")
    opp = _make_opportunity(db, "op-assert-1")
    sdoc, erun = _make_extraction_context(db, src)

    fa = assert_field(
        db,
        opportunity_id=opp.id,
        field_path="title",
        value="Postdoc in HEP",
        raw_value="Postdoc in HEP",
        confidence=0.99,
        derivation=Derivation.OFFICIAL_API,
        source_document_id=sdoc.id,
        extraction_run_id=erun.id,
        json_pointer="/metadata/position",
    )

    assert fa.is_current is True
    assert fa.opportunity_id == opp.id
    assert fa.field_path == "title"
    assert fa.normalized_value == "Postdoc in HEP"
    assert fa.confidence == pytest.approx(0.99)


def test_assert_field_second_call_supersedes_first(db: Session) -> None:
    src = _make_source(db, "Source B")
    opp = _make_opportunity(db, "op-assert-2")
    sdoc, erun = _make_extraction_context(db, src)

    first = assert_field(
        db,
        opportunity_id=opp.id,
        field_path="title",
        value="Original Title",
        raw_value="Original Title",
        confidence=0.90,
        derivation=Derivation.OFFICIAL_API,
        source_document_id=sdoc.id,
        extraction_run_id=erun.id,
        json_pointer="/metadata/position",
    )

    second = assert_field(
        db,
        opportunity_id=opp.id,
        field_path="title",
        value="Updated Title",
        raw_value="Updated Title",
        confidence=0.95,
        derivation=Derivation.OFFICIAL_API,
        source_document_id=sdoc.id,
        extraction_run_id=erun.id,
        json_pointer="/metadata/position",
    )

    db.refresh(first)
    assert first.is_current is False
    assert second.is_current is True


def test_assert_field_ungrounded_raises_for_official_api(db: Session) -> None:
    src = _make_source(db, "Source C")
    opp = _make_opportunity(db, "op-assert-3")
    sdoc, erun = _make_extraction_context(db, src)

    with pytest.raises(UngroundedAssertion):
        assert_field(
            db,
            opportunity_id=opp.id,
            field_path="title",
            value="Some Title",
            raw_value="Some Title",
            confidence=0.90,
            derivation=Derivation.OFFICIAL_API,
            source_document_id=sdoc.id,
            extraction_run_id=erun.id,
            # Neither json_pointer nor text_start provided
        )


def test_assert_field_human_derivation_allowed_without_grounding(db: Session) -> None:
    src = _make_source(db, "Source D")
    opp = _make_opportunity(db, "op-assert-4")
    sdoc, erun = _make_extraction_context(db, src)

    fa = assert_field(
        db,
        opportunity_id=opp.id,
        field_path="country",
        value="CH",
        raw_value=None,
        confidence=0.85,
        derivation=Derivation.HUMAN,
        source_document_id=sdoc.id,
        extraction_run_id=erun.id,
        # No json_pointer or text_start — allowed for HUMAN
    )

    assert fa.is_current is True
    assert fa.derivation == Derivation.HUMAN


def test_assert_field_inherited_derivation_allowed_without_grounding(db: Session) -> None:
    src = _make_source(db, "Source E")
    opp = _make_opportunity(db, "op-assert-5")
    sdoc, erun = _make_extraction_context(db, src)

    fa = assert_field(
        db,
        opportunity_id=opp.id,
        field_path="primary_language",
        value="en",
        raw_value=None,
        confidence=0.80,
        derivation=Derivation.INHERITED,
        source_document_id=sdoc.id,
        extraction_run_id=erun.id,
    )

    assert fa.is_current is True


def test_assert_field_wrong_offsets_raises_ungrounded(db: Session) -> None:
    src = _make_source(db, "Source F")
    opp = _make_opportunity(db, "op-assert-6")
    sdoc, erun = _make_extraction_context(db, src)

    source_text = "Postdoc in High Energy Physics at CERN"

    with pytest.raises(UngroundedAssertion):
        assert_field(
            db,
            opportunity_id=opp.id,
            field_path="title",
            value="Postdoc in HEP",
            raw_value="Postdoc in HEP",
            confidence=0.90,
            derivation=Derivation.PARSER,
            source_document_id=sdoc.id,
            extraction_run_id=erun.id,
            text_start=0,
            text_end=5,  # window is "Postd" — "Postdoc in HEP" not in it
            source_text=source_text,
        )


def test_assert_field_correct_offsets_pass_verification(db: Session) -> None:
    src = _make_source(db, "Source G")
    opp = _make_opportunity(db, "op-assert-7")
    sdoc, erun = _make_extraction_context(db, src)

    source_text = "Postdoc in High Energy Physics at CERN"

    fa = assert_field(
        db,
        opportunity_id=opp.id,
        field_path="title",
        value="Postdoc in High Energy Physics at CERN",
        raw_value="Postdoc in High Energy Physics at CERN",
        confidence=0.90,
        derivation=Derivation.PARSER,
        source_document_id=sdoc.id,
        extraction_run_id=erun.id,
        text_start=0,
        text_end=38,
        source_text=source_text,
    )

    assert fa.is_current is True


def test_assert_field_only_latest_is_current_for_same_field(db: Session) -> None:
    src = _make_source(db, "Source H")
    opp = _make_opportunity(db, "op-assert-8")
    sdoc, erun = _make_extraction_context(db, src)

    for i in range(3):
        assert_field(
            db,
            opportunity_id=opp.id,
            field_path="description",
            value=f"Description {i}",
            raw_value=f"Description {i}",
            confidence=0.80,
            derivation=Derivation.OFFICIAL_API,
            source_document_id=sdoc.id,
            extraction_run_id=erun.id,
            json_pointer="/metadata/description",
        )

    current = (
        db.query(FieldAssertion)
        .filter_by(opportunity_id=opp.id, field_path="description", is_current=True)
        .all()
    )
    assert len(current) == 1
    assert current[0].normalized_value == "Description 2"
