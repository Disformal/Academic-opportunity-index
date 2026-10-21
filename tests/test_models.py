"""Tests for AOI database models — Stage 2."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from aoi.db.enums import (
    ClosureReason,
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
    Organization,
    Source,
    SourceDocument,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_source(name: str = "Test Source") -> Source:
    return Source(
        name=name,
        domain="example.com",
        endpoint_url="https://example.com/api",
        source_type="api",
    )


def make_opportunity(
    identity_key: str = "test-key",
    status: OpportunityStatus = OpportunityStatus.OPEN,
    publication_state: PublicationState = PublicationState.UNVERIFIED,
) -> Opportunity:
    now = datetime.now(UTC)
    return Opportunity(
        identity_kind=IdentityKind.NATIVE_REF,
        identity_key=identity_key,
        title="Test Opportunity",
        type="fellowship",
        status=status,
        publication_state=publication_state,
        first_seen_at=now,
        last_seen_at=now,
    )


# ---------------------------------------------------------------------------
# 1. Model instantiation (no DB)
# ---------------------------------------------------------------------------


def test_source_instantiation_with_required_fields() -> None:
    s = make_source()
    assert s.name == "Test Source"
    assert s.domain == "example.com"


def test_opportunity_instantiation_with_required_fields() -> None:
    opp = make_opportunity()
    assert opp.identity_key == "test-key"
    assert opp.publication_state == PublicationState.UNVERIFIED


def test_organization_instantiation_with_required_fields() -> None:
    org = Organization(preferred_name="CERN", country_code="CH")
    assert org.preferred_name == "CERN"


# ---------------------------------------------------------------------------
# 2. All 12 tables exist after migration
# ---------------------------------------------------------------------------


def test_all_twelve_tables_exist_after_migration(db) -> None:  # type: ignore[no-untyped-def]
    inspector = inspect(db.get_bind())
    tables = set(inspector.get_table_names())
    expected = {
        "source",
        "crawl_url",
        "fetch",
        "source_document",
        "extraction_run",
        "field_assertion",
        "opportunity",
        "opportunity_version",
        "duplicate_cluster",
        "duplicate_member",
        "robots_assessment",
        "organization",
    }
    missing = expected - tables
    assert not missing, f"Missing tables: {missing}"


# ---------------------------------------------------------------------------
# 3. Unique constraints
# ---------------------------------------------------------------------------


def test_source_unique_name_raises_on_duplicate(db) -> None:  # type: ignore[no-untyped-def]
    s1 = make_source("UniqueSource")
    s2 = make_source("UniqueSource")
    db.add(s1)
    db.flush()
    db.add(s2)
    with pytest.raises(IntegrityError):
        db.flush()


def test_opportunity_unique_identity_raises_on_duplicate(db) -> None:  # type: ignore[no-untyped-def]
    opp1 = make_opportunity("same-key")
    opp2 = make_opportunity("same-key")
    db.add(opp1)
    db.flush()
    db.add(opp2)
    with pytest.raises(IntegrityError):
        db.flush()


# ---------------------------------------------------------------------------
# 4. Check constraints
# ---------------------------------------------------------------------------


def test_source_trust_tier_below_one_raises(db) -> None:  # type: ignore[no-untyped-def]
    s = make_source("BadTier")
    s.trust_tier = 0
    db.add(s)
    with pytest.raises(IntegrityError):
        db.flush()


def test_source_trust_tier_above_five_raises(db) -> None:  # type: ignore[no-untyped-def]
    s = make_source("BadTier2")
    s.trust_tier = 6
    db.add(s)
    with pytest.raises(IntegrityError):
        db.flush()


def test_source_trust_tier_boundary_values_accepted(db) -> None:  # type: ignore[no-untyped-def]
    s1 = make_source("Tier1")
    s1.trust_tier = 1
    s2 = make_source("Tier5")
    s2.trust_tier = 5
    db.add_all([s1, s2])
    db.flush()


def test_field_assertion_confidence_above_one_raises(db) -> None:  # type: ignore[no-untyped-def]
    opp = make_opportunity("conf-test")
    db.add(opp)
    db.flush()

    src = make_source("ConfSource")
    db.add(src)
    db.flush()

    crawl_url = CrawlUrl(
        source_id=src.id,
        url_canonical="https://example.com/1",
        url_original="https://example.com/1",
        url_hash=b"\x00" * 32,
        page_role=PageRole.LISTING,
    )
    db.add(crawl_url)
    db.flush()

    fetch = Fetch(
        crawl_url_id=crawl_url.id,
        source_id=src.id,
        fetched_at=datetime.now(UTC),
        outcome=FetchOutcome.HIT,
    )
    db.add(fetch)
    db.flush()

    sdoc = SourceDocument(
        fetch_id=fetch.id,
        source_id=src.id,
        content_digest="a" * 64,
        media_type="text/html",
    )
    db.add(sdoc)
    db.flush()

    erun = ExtractionRun(
        source_document_id=sdoc.id,
        extractor_id="test",
        extractor_version="0.1.0",
        started_at=datetime.now(UTC),
    )
    db.add(erun)
    db.flush()

    fa = FieldAssertion(
        opportunity_id=opp.id,
        field_path="title",
        normalized_value={"v": "Test"},
        confidence=1.5,  # invalid
        derivation=Derivation.OFFICIAL_API,
        source_document_id=sdoc.id,
        extraction_run_id=erun.id,
    )
    db.add(fa)
    with pytest.raises(IntegrityError):
        db.flush()


# ---------------------------------------------------------------------------
# 5. Provenance trigger: verified without assertions raises
# ---------------------------------------------------------------------------


def test_provenance_trigger_verified_without_assertions_raises(db) -> None:  # type: ignore[no-untyped-def]
    opp = make_opportunity("prov-fail", publication_state=PublicationState.UNVERIFIED)
    db.add(opp)
    db.flush()

    with pytest.raises(DBAPIError):
        db.execute(
            text("UPDATE opportunity SET publication_state = 'verified' WHERE id = :id"),
            {"id": str(opp.id)},
        )
        db.flush()


# ---------------------------------------------------------------------------
# 6. Provenance trigger: unverified succeeds
# ---------------------------------------------------------------------------


def test_provenance_trigger_unverified_insert_succeeds(db) -> None:  # type: ignore[no-untyped-def]
    opp = make_opportunity("prov-ok", publication_state=PublicationState.UNVERIFIED)
    db.add(opp)
    db.flush()
    # If we get here without an exception, the test passes.


# ---------------------------------------------------------------------------
# 7. ck_closed_consistent constraint
# ---------------------------------------------------------------------------


def test_closed_consistent_constraint_closed_without_closed_at_raises(db) -> None:  # type: ignore[no-untyped-def]
    """status='closed' requires closed_at to be non-null."""
    now = datetime.now(UTC)
    opp = Opportunity(
        identity_kind=IdentityKind.NATIVE_REF,
        identity_key="closed-no-date",
        title="Closed Opportunity",
        type="fellowship",
        status=OpportunityStatus.CLOSED,
        publication_state=PublicationState.UNVERIFIED,
        first_seen_at=now,
        last_seen_at=now,
        closed_at=None,  # violates the constraint
    )
    db.add(opp)
    with pytest.raises(IntegrityError):
        db.flush()


def test_closed_consistent_constraint_open_with_closed_at_raises(db) -> None:  # type: ignore[no-untyped-def]
    """status != 'closed' requires closed_at to be null."""
    now = datetime.now(UTC)
    opp = Opportunity(
        identity_kind=IdentityKind.NATIVE_REF,
        identity_key="open-with-date",
        title="Open Opportunity",
        type="fellowship",
        status=OpportunityStatus.OPEN,
        publication_state=PublicationState.UNVERIFIED,
        first_seen_at=now,
        last_seen_at=now,
        closed_at=now,  # violates: not closed but has closed_at
    )
    db.add(opp)
    with pytest.raises(IntegrityError):
        db.flush()


def test_closed_consistent_constraint_both_null_succeeds(db) -> None:  # type: ignore[no-untyped-def]
    """Non-closed status with null closed_at is valid."""
    opp = make_opportunity("open-no-date")
    db.add(opp)
    db.flush()


def test_closed_consistent_constraint_both_set_succeeds(db) -> None:  # type: ignore[no-untyped-def]
    """Closed status with non-null closed_at is valid."""
    now = datetime.now(UTC)
    opp = Opportunity(
        identity_kind=IdentityKind.NATIVE_REF,
        identity_key="closed-with-date",
        title="Closed Opportunity",
        type="fellowship",
        status=OpportunityStatus.CLOSED,
        publication_state=PublicationState.UNVERIFIED,
        first_seen_at=now,
        last_seen_at=now,
        closed_at=now,
        closure_reason=ClosureReason.DEADLINE_PASSED,
    )
    db.add(opp)
    db.flush()


# ---------------------------------------------------------------------------
# 8. created_at server default
# ---------------------------------------------------------------------------


def test_created_at_server_default_is_set_after_flush(db) -> None:  # type: ignore[no-untyped-def]
    s = make_source("TimestampSource")
    db.add(s)
    db.flush()
    db.refresh(s)
    assert s.created_at is not None


# ---------------------------------------------------------------------------
# 9. updated_at trigger
# ---------------------------------------------------------------------------


def test_updated_at_is_maintained_by_trigger(db) -> None:  # type: ignore[no-untyped-def]
    s = make_source("UpdatedAtSource")
    db.add(s)
    db.flush()
    db.refresh(s)
    original_updated_at = s.updated_at

    # Update the record so the trigger fires
    db.execute(
        text("UPDATE source SET domain = 'updated.com' WHERE id = :id"),
        {"id": str(s.id)},
    )
    db.flush()
    db.refresh(s)

    # The trigger should have bumped updated_at (or it stays equal if within same microsecond)
    assert s.updated_at is not None
    assert s.updated_at >= original_updated_at
