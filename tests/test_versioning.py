"""Tests for opportunity versioning — aoi.pipeline.versioning. Uses real DB."""

from datetime import UTC, datetime

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
    Opportunity,
    Source,
    SourceDocument,
)
from aoi.pipeline.assertions import assert_field
from aoi.pipeline.versioning import maybe_version, snapshot

# ---------------------------------------------------------------------------
# Helpers  (same pattern as test_assertions.py)
# ---------------------------------------------------------------------------


def _make_source(db: Session, name: str = "Versioning Source") -> Source:
    src = Source(
        name=name,
        domain="example.com",
        endpoint_url="https://example.com/api",
        source_type="api",
    )
    db.add(src)
    db.flush()
    return src


def _make_opportunity(db: Session, key: str) -> Opportunity:
    now = datetime.now(UTC)
    opp = Opportunity(
        identity_kind=IdentityKind.NATIVE_REF,
        identity_key=key,
        title="Test Opportunity",
        type="postdoc",
        status=OpportunityStatus.OPEN,
        publication_state=PublicationState.UNVERIFIED,
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(opp)
    db.flush()
    return opp


def _make_extraction_context(
    db: Session, source: Source, digest: str = "a" * 64
) -> tuple[SourceDocument, ExtractionRun]:
    crawl_url = CrawlUrl(
        source_id=source.id,
        url_canonical="https://example.com/job/v",
        url_original="https://example.com/job/v",
        url_hash=b"\x01" * 32,
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
        content_digest=digest,
        media_type="application/json",
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

    return sdoc, erun


def _add_assertion(
    db: Session,
    opp: Opportunity,
    sdoc: SourceDocument,
    erun: ExtractionRun,
    field_path: str,
    value: str,
) -> None:
    assert_field(
        db,
        opportunity_id=opp.id,
        field_path=field_path,
        value=value,
        raw_value=value,
        confidence=0.95,
        derivation=Derivation.OFFICIAL_API,
        source_document_id=sdoc.id,
        extraction_run_id=erun.id,
        json_pointer=f"/metadata/{field_path}",
    )


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_returns_current_field_assertions(self, db: Session) -> None:
        src = _make_source(db, "Snap Src A")
        opp = _make_opportunity(db, "snap-1")
        sdoc, erun = _make_extraction_context(db, src)

        _add_assertion(db, opp, sdoc, erun, "title", "Postdoc in HEP")
        _add_assertion(db, opp, sdoc, erun, "country", "CH")

        snap = snapshot(db, opp.id)

        assert snap["title"] == "Postdoc in HEP"
        assert snap["country"] == "CH"

    def test_snapshot_excludes_superseded_assertions(self, db: Session) -> None:
        src = _make_source(db, "Snap Src B")
        opp = _make_opportunity(db, "snap-2")
        sdoc, erun = _make_extraction_context(db, src)

        _add_assertion(db, opp, sdoc, erun, "title", "Old Title")
        _add_assertion(db, opp, sdoc, erun, "title", "New Title")

        snap = snapshot(db, opp.id)

        assert snap["title"] == "New Title"

    def test_snapshot_empty_when_no_assertions(self, db: Session) -> None:
        opp = _make_opportunity(db, "snap-3")
        snap = snapshot(db, opp.id)
        assert snap == {}


# ---------------------------------------------------------------------------
# maybe_version
# ---------------------------------------------------------------------------


class TestMaybeVersion:
    def test_creates_version_on_first_call(self, db: Session) -> None:
        src = _make_source(db, "Ver Src A")
        opp = _make_opportunity(db, "ver-1")
        sdoc, erun = _make_extraction_context(db, src)
        _add_assertion(db, opp, sdoc, erun, "title", "Postdoc in HEP")

        version = maybe_version(
            db, opp.id, extraction_run_id=erun.id, change_reason="new_extraction"
        )

        assert version is not None
        assert version.version_number == 1
        assert version.change_reason == "new_extraction"
        assert version.payload["title"] == "Postdoc in HEP"

    def test_no_version_when_unchanged(self, db: Session) -> None:
        src = _make_source(db, "Ver Src B")
        opp = _make_opportunity(db, "ver-2")
        sdoc, erun = _make_extraction_context(db, src)
        _add_assertion(db, opp, sdoc, erun, "title", "Postdoc in HEP")

        v1 = maybe_version(db, opp.id, extraction_run_id=erun.id, change_reason="new_extraction")
        assert v1 is not None

        # Same assertions, call again
        v2 = maybe_version(db, opp.id, extraction_run_id=erun.id, change_reason="new_extraction")
        assert v2 is None

    def test_version_number_increments(self, db: Session) -> None:
        src = _make_source(db, "Ver Src C")
        opp = _make_opportunity(db, "ver-3")
        sdoc, erun = _make_extraction_context(db, src)

        _add_assertion(db, opp, sdoc, erun, "title", "Original Title")
        v1 = maybe_version(db, opp.id, extraction_run_id=erun.id, change_reason="new_extraction")
        assert v1 is not None
        assert v1.version_number == 1

        # Update the field
        _add_assertion(db, opp, sdoc, erun, "title", "Updated Title")
        v2 = maybe_version(db, opp.id, extraction_run_id=erun.id, change_reason="field_update")
        assert v2 is not None
        assert v2.version_number == 2
        assert v2.payload["title"] == "Updated Title"

    def test_version_records_extraction_run(self, db: Session) -> None:
        src = _make_source(db, "Ver Src D")
        opp = _make_opportunity(db, "ver-4")
        sdoc, erun = _make_extraction_context(db, src)
        _add_assertion(db, opp, sdoc, erun, "title", "Test")

        version = maybe_version(
            db, opp.id, extraction_run_id=erun.id, change_reason="new_extraction"
        )
        assert version is not None
        assert version.caused_by_extraction_run_id == erun.id

    def test_change_summary_lists_changed_fields(self, db: Session) -> None:
        src = _make_source(db, "Ver Src E")
        opp = _make_opportunity(db, "ver-5")
        sdoc, erun = _make_extraction_context(db, src)

        _add_assertion(db, opp, sdoc, erun, "title", "Postdoc in HEP")
        _add_assertion(db, opp, sdoc, erun, "country", "CH")
        maybe_version(db, opp.id, extraction_run_id=erun.id, change_reason="new_extraction")

        # Update country only
        _add_assertion(db, opp, sdoc, erun, "country", "DE")
        v2 = maybe_version(db, opp.id, extraction_run_id=erun.id, change_reason="field_update")
        assert v2 is not None
        assert "country" in v2.change_summary
