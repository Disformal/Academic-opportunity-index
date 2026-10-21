"""Tests for the M1 core schema (ADR 0007): evidence, core, analytics, ops."""

import hashlib
import hmac
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from aoi.db.schema import LayerBase
from aoi.db.schema.core import (
    Assertion,
    Award,
    CallDetails,
    Discipline,
    DisciplineParent,
    ExtractionRun,
    FellowshipDetails,
    Funder,
    JobDetails,
    OrgAlias,
    Organization,
    PersonPseudonym,
    Position,
    PositionHistory,
    PositionLink,
    Posting,
    PostingHistory,
    PostingSighting,
    Source,
)
from aoi.db.schema.enums import (
    AliasMethod,
    AssertionMethod,
    DisciplineScheme,
    FetchOutcome,
    LinkMethod,
    OpportunityKind,
    OrgStatus,
    PersonKeyKind,
    PostingIdentityKind,
    PostingState,
    SalaryPeriod,
    SourceKind,
    TargetRole,
)
from aoi.db.schema.evidence import Fetch
from aoi.db.schema.ops import CrawlTarget, Host, Label, Sample, SampleItem

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _source(db: Session, name: str = "inspire") -> Source:
    source = Source(name=name, kind=SourceKind.API, base_url="https://inspirehep.net/api/jobs")
    db.add(source)
    db.flush()
    return source


def _fetch(db: Session, source: Source) -> Fetch:
    host = Host(hostname=f"{source.name}.example.org")
    db.add(host)
    db.flush()
    url = f"https://{host.hostname}/jobs/1"
    target = CrawlTarget(
        source_id=source.id,
        host_id=host.id,
        url=url,
        url_sha256=hashlib.sha256(url.encode()).hexdigest(),
        role=TargetRole.DETAIL,
    )
    db.add(target)
    db.flush()
    fetch = Fetch(crawl_target_id=target.id, url=url, requested_at=NOW, outcome=FetchOutcome.HIT)
    db.add(fetch)
    db.flush()
    return fetch


def _posting(db: Session, source: Source, key: str = "2871234") -> Posting:
    posting = Posting(
        source_id=source.id,
        identity_kind=PostingIdentityKind.NATIVE_REF,
        identity_key=key,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    db.add(posting)
    db.flush()
    return posting


def _run(db: Session, fetch: Fetch) -> ExtractionRun:
    run = ExtractionRun(
        fetch_id=fetch.id, extractor="inspire.api", extractor_version="1.0.0", started_at=NOW
    )
    db.add(run)
    db.flush()
    return run


def _assertion(posting: Posting, fetch: Fetch, run: ExtractionRun, **kw: Any) -> Assertion:
    fields: dict[str, Any] = {
        "field": "title",
        "value": "Postdoc",
        "method": AssertionMethod.OFFICIAL_API,
        "pointer": "/metadata/position",
        "confidence": 1,
        "observed_at": NOW,
    }
    fields.update(kw)
    return Assertion(posting_id=posting.id, fetch_id=fetch.id, extraction_run_id=run.id, **fields)


@pytest.fixture()
def chain(db: Session) -> tuple[Posting, Fetch, ExtractionRun]:
    source = _source(db)
    fetch = _fetch(db, source)
    return _posting(db, source), fetch, _run(db, fetch)


def test_layer_schemas_exist_after_upgrade(db: Session) -> None:
    rows = db.execute(
        text(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name IN ('evidence', 'core', 'analytics', 'ops')"
        )
    ).scalars()
    assert set(rows) == {"evidence", "core", "analytics", "ops"}


# --- assertion evidence pointers (the ported provenance rule) -----------------------------


def test_assertion_accepted_when_api_value_has_pointer(db: Session, chain: tuple) -> None:
    db.add(_assertion(*chain))
    db.flush()


@pytest.mark.parametrize(
    "warc",
    [
        {"warc_key": "warc/2026/10/18/x.warc.gz"},
        {"warc_record_id": "<urn:uuid:0d3bf6c2-1b42-4b37-9074-620f6b8b4826>"},
    ],
)
def test_fetch_rejected_when_only_one_of_warc_key_and_record_id_is_set(
    db: Session, warc: dict[str, str]
) -> None:
    target_id = _fetch(db, _source(db)).crawl_target_id
    db.add(
        Fetch(
            crawl_target_id=target_id,
            url="https://inspire.example.org/jobs/1",
            requested_at=NOW,
            outcome=FetchOutcome.HIT,
            **warc,
        )
    )
    with pytest.raises(IntegrityError, match="ck_fetch_warc_pair"):
        db.flush()


def test_assertion_rejected_when_api_value_has_no_pointer(db: Session, chain: tuple) -> None:
    db.add(_assertion(*chain, pointer=None))
    with pytest.raises(IntegrityError, match="ck_assertion_evidence_pointer"):
        db.flush()


def test_assertion_rejected_when_llm_value_has_no_quote(db: Session, chain: tuple) -> None:
    db.add(_assertion(*chain, method=AssertionMethod.LLM, pointer=None))
    with pytest.raises(IntegrityError, match="ck_assertion_evidence_pointer"):
        db.flush()


def test_assertion_rejected_when_span_has_start_but_no_end(db: Session, chain: tuple) -> None:
    db.add(_assertion(*chain, method=AssertionMethod.TEMPLATE, pointer=None, span_start=10))
    with pytest.raises(IntegrityError, match="ck_assertion_span"):
        db.flush()


def test_assertion_rejected_when_confidence_above_one(db: Session, chain: tuple) -> None:
    db.add(_assertion(*chain, confidence=1.5))
    with pytest.raises(IntegrityError, match="ck_assertion_confidence"):
        db.flush()


# --- posting activation needs provenance --------------------------------------------------


def test_posting_cannot_be_inserted_active(db: Session) -> None:
    source = _source(db)
    db.add(
        Posting(
            source_id=source.id,
            identity_kind=PostingIdentityKind.NATIVE_REF,
            identity_key="1",
            state=PostingState.ACTIVE,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    )
    with pytest.raises(IntegrityError, match="title"):
        db.flush()


def test_posting_cannot_become_active_without_application_url_assertion(
    db: Session, chain: tuple
) -> None:
    posting, fetch, run = chain
    db.add(_assertion(posting, fetch, run))  # title only
    db.flush()
    posting.state = PostingState.ACTIVE
    with pytest.raises(IntegrityError, match="application_url"):
        db.flush()


def test_posting_becomes_active_with_title_and_application_url_assertions(
    db: Session, chain: tuple
) -> None:
    posting, fetch, run = chain
    db.add(_assertion(posting, fetch, run))
    db.add(
        _assertion(
            posting,
            fetch,
            run,
            field="application_url",
            value="https://example.org/apply",
            pointer="/metadata/urls/0/value",
        )
    )
    db.flush()
    posting.state = PostingState.ACTIVE
    db.flush()
    db.refresh(posting)
    assert posting.state == PostingState.ACTIVE


# --- posting identity and lifecycle -------------------------------------------------------


def test_posting_identity_unique_within_source(db: Session) -> None:
    source = _source(db)
    _posting(db, source, key="42")
    with pytest.raises(IntegrityError, match="uq_posting_source_identity"):
        _posting(db, source, key="42")


def test_posting_identity_may_repeat_across_sources(db: Session) -> None:
    _posting(db, _source(db, "inspire"), key="42")
    _posting(db, _source(db, "euraxess"), key="42")


def test_posting_closed_requires_reason(db: Session) -> None:
    posting = _posting(db, _source(db))
    posting.state = PostingState.CLOSED
    with pytest.raises(IntegrityError, match="ck_posting_closure_reason"):
        db.flush()


# --- crawl targets and extraction runs ----------------------------------------------------


@pytest.mark.parametrize("interval", [timedelta(minutes=30), timedelta(days=8)])
def test_crawl_target_rejects_recrawl_interval_outside_bounds(
    db: Session, interval: timedelta
) -> None:
    fetch = _fetch(db, _source(db))
    target = db.get(CrawlTarget, fetch.crawl_target_id)
    assert target is not None
    target.recrawl_interval = interval
    with pytest.raises(IntegrityError, match="ck_crawl_target_recrawl_bounds"):
        db.flush()


def test_extraction_run_rejected_when_the_same_version_extracts_a_fetch_twice(
    db: Session, chain: tuple
) -> None:
    _, fetch, _ = chain
    db.add(
        ExtractionRun(
            fetch_id=fetch.id, extractor="inspire.api", extractor_version="1.0.0", started_at=NOW
        )
    )
    with pytest.raises(IntegrityError, match="uq_extraction_run_fetch_version"):
        db.flush()


def test_extraction_run_accepted_for_a_new_version_of_the_same_extractor(
    db: Session, chain: tuple
) -> None:
    _, fetch, _ = chain
    db.add(
        ExtractionRun(
            fetch_id=fetch.id, extractor="inspire.api", extractor_version="1.1.0", started_at=NOW
        )
    )
    db.flush()


def test_extraction_run_rejects_model_without_prompt_hash(db: Session) -> None:
    fetch = _fetch(db, _source(db))
    db.add(
        ExtractionRun(
            fetch_id=fetch.id,
            extractor="generic.llm",
            extractor_version="0.1.0",
            model_id="qwen3:8b",
            started_at=NOW,
        )
    )
    with pytest.raises(IntegrityError, match="ck_extraction_run_llm_fields"):
        db.flush()


def test_posting_updated_at_maintained_by_trigger(db: Session) -> None:
    posting = _posting(db, _source(db))
    stamp = db.execute(
        text(
            "UPDATE core.posting SET updated_at = '2000-01-01' WHERE id = :id RETURNING updated_at"
        ),
        {"id": posting.id},
    ).scalar_one()
    assert stamp.year > 2000


# --- the models and the migrations describe the same schema ------------------------------


def test_layer_models_match_migrated_database(engine: Engine) -> None:
    layer_schemas = {"evidence", "core", "analytics", "ops"}

    def include_name(name: str | None, type_: str, parent_names: object) -> bool:
        return name in layer_schemas if type_ == "schema" else True

    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={
                "include_schemas": True,
                "include_name": include_name,
                "compare_type": True,
                "compare_server_default": True,
            },
        )
        diff = compare_metadata(context, LayerBase.metadata)
    assert diff == []


# --- organizations (ROR mirror) and aliases -----------------------------------------------


def _org(db: Session, ror_id: str = "01ggx4157") -> Organization:
    org = Organization(
        ror_id=ror_id,
        display_name="European Organization for Nuclear Research",
        names=[{"value": "CERN", "types": ["acronym"], "lang": None}],
        types=["facility"],
        status=OrgStatus.ACTIVE,
        ror_release="v1.55",
    )
    db.add(org)
    db.flush()
    return org


def _alias(raw: str, **kw: Any) -> OrgAlias:
    fields: dict[str, Any] = {
        "ror_id": "01ggx4157",
        "method": AliasMethod.ROR_API,
        "confidence": 0.95,
        "decided_by": "ror-affiliation@2",
    }
    fields.update(kw)
    return OrgAlias(raw=raw, **fields)


def test_organization_rejects_ror_url_as_id(db: Session) -> None:
    with pytest.raises(DataError):
        _org(db, ror_id="https://ror.org/01ggx4157")


@pytest.mark.parametrize("ror_id", ["01GGX4157", "11ggx4157", "01ggx41ab"])
def test_organization_rejects_malformed_ror_id(db: Session, ror_id: str) -> None:
    with pytest.raises(IntegrityError, match="ck_organization_ror_id"):
        _org(db, ror_id=ror_id)


def test_org_alias_allows_one_current_mapping_per_raw_string(db: Session) -> None:
    _org(db)
    db.add(_alias("CERN, Geneva"))
    db.flush()
    db.add(_alias("CERN, Geneva", method=AliasMethod.HUMAN, decided_by="reviewer"))
    with pytest.raises(IntegrityError, match="uq_org_alias_current_raw"):
        db.flush()


def test_org_alias_accepts_new_mapping_after_superseding_old(db: Session) -> None:
    _org(db)
    old = _alias("Cantabria U.")
    db.add(old)
    db.flush()
    old.superseded_at = NOW
    db.add(
        _alias(
            "Cantabria U.",
            ror_id=None,
            method=AliasMethod.HUMAN,
            confidence=None,
            decided_by="reviewer",
        )
    )
    db.flush()


def test_org_alias_requires_confidence_for_automatic_match(db: Session) -> None:
    _org(db)
    db.add(_alias("CERN", confidence=None))
    with pytest.raises(IntegrityError, match="ck_org_alias_confidence"):
        db.flush()


# --- positions, links, history and details ------------------------------------------------


def _position(db: Session, kind: OpportunityKind = OpportunityKind.JOB) -> Position:
    position = Position(kind=kind, title="Postdoctoral Fellow in Lattice QCD")
    db.add(position)
    db.flush()
    return position


def _link(posting: Posting, position: Position, **kw: Any) -> PositionLink:
    fields: dict[str, Any] = {
        "method": LinkMethod.AUTO,
        "probability": 0.97,
        "decided_by": "linker@0.1",
    }
    fields.update(kw)
    return PositionLink(posting_id=posting.id, position_id=position.id, **fields)


def test_position_link_allows_one_current_link_per_posting(db: Session) -> None:
    posting = _posting(db, _source(db))
    db.add(_link(posting, _position(db)))
    db.flush()
    db.add(_link(posting, _position(db)))
    with pytest.raises(IntegrityError, match="uq_position_link_current_posting"):
        db.flush()


def test_position_link_relinks_after_superseding(db: Session) -> None:
    posting = _posting(db, _source(db))
    first = _link(posting, _position(db))
    db.add(first)
    db.flush()
    first.superseded_at = NOW
    db.add(
        _link(
            posting,
            _position(db),
            method=LinkMethod.HUMAN,
            probability=None,
            decided_by="reviewer",
        )
    )
    db.flush()


def test_position_link_auto_requires_probability(db: Session) -> None:
    db.add(_link(_posting(db, _source(db)), _position(db), probability=None))
    with pytest.raises(IntegrityError, match="ck_position_link_probability"):
        db.flush()


def test_position_deadline_requires_precision(db: Session) -> None:
    db.add(Position(kind=OpportunityKind.JOB, title="PhD Student", deadline_at=NOW))
    with pytest.raises(IntegrityError, match="ck_position_deadline_precision"):
        db.flush()


def test_posting_history_allows_one_current_version(db: Session) -> None:
    posting = _posting(db, _source(db))
    for start in (NOW, NOW + timedelta(days=1)):
        db.add(PostingHistory(posting_id=posting.id, observed_from=start, snapshot={}))
    with pytest.raises(IntegrityError, match="uq_posting_history_current"):
        db.flush()


def test_posting_history_rejects_observed_to_before_from(db: Session) -> None:
    posting = _posting(db, _source(db))
    db.add(
        PostingHistory(
            posting_id=posting.id,
            observed_from=NOW,
            observed_to=NOW - timedelta(hours=1),
            snapshot={},
        )
    )
    with pytest.raises(IntegrityError, match="ck_posting_history_order"):
        db.flush()


def test_job_details_rejected_for_fellowship_position(db: Session) -> None:
    position = _position(db, OpportunityKind.FELLOWSHIP)
    db.add(JobDetails(position_id=position.id))
    with pytest.raises(IntegrityError, match="fkey|foreign key"):
        db.flush()


def test_fellowship_details_accepted_for_scholarship_position(db: Session) -> None:
    position = _position(db, OpportunityKind.SCHOLARSHIP)
    db.add(FellowshipDetails(position_id=position.id, kind=OpportunityKind.SCHOLARSHIP))
    db.flush()


def test_fellowship_details_rejected_for_job_position(db: Session) -> None:
    position = _position(db, OpportunityKind.JOB)
    db.add(FellowshipDetails(position_id=position.id, kind=OpportunityKind.JOB))
    with pytest.raises(IntegrityError, match="ck_fellowship_details_kind"):
        db.flush()


def test_job_details_rejects_salary_without_currency(db: Session) -> None:
    position = _position(db)
    db.add(JobDetails(position_id=position.id, salary_min=50000, salary_period=SalaryPeriod.YEAR))
    with pytest.raises(IntegrityError, match="ck_job_details_salary_unit"):
        db.flush()


def test_job_details_rejects_salary_max_below_min(db: Session) -> None:
    position = _position(db)
    db.add(
        JobDetails(
            position_id=position.id,
            salary_min=60000,
            salary_max=50000,
            salary_currency="EUR",
            salary_period=SalaryPeriod.YEAR,
        )
    )
    with pytest.raises(IntegrityError, match="ck_job_details_salary_order"):
        db.flush()


# --- funding calls, funders, awards and pseudonyms ----------------------------------------


def test_call_details_rejected_for_job_position(db: Session) -> None:
    position = _position(db, OpportunityKind.JOB)
    db.add(CallDetails(position_id=position.id))
    with pytest.raises(IntegrityError, match="fkey|foreign key"):
        db.flush()


def test_call_details_rejects_funding_without_currency(db: Session) -> None:
    position = _position(db, OpportunityKind.FUNDING_CALL)
    db.add(CallDetails(position_id=position.id, funding_max=1_500_000))
    with pytest.raises(IntegrityError, match="ck_call_details_funding_currency"):
        db.flush()


def test_funder_requires_ror_or_crossref_id(db: Session) -> None:
    db.add(Funder(name="European Research Council"))
    with pytest.raises(IntegrityError, match="ck_funder_has_key"):
        db.flush()


def test_award_unique_per_source_native_id(db: Session) -> None:
    for _ in range(2):
        db.add(Award(source="cordis", native_id="101042368"))
    with pytest.raises(IntegrityError, match="uq_award_source_native_id"):
        db.flush()


def test_award_rejects_end_before_start(db: Session) -> None:
    db.add(
        Award(
            source="cordis",
            native_id="1",
            start_date=date(2027, 1, 1),
            end_date=date(2026, 1, 1),
        )
    )
    with pytest.raises(IntegrityError, match="ck_award_dates"):
        db.flush()


@pytest.mark.parametrize(
    "pid",
    [
        "0000-0002-1825-0097",  # a raw ORCID must never be stored as a pseudonym
        "A" * 64,  # upper-case hex
        "a" * 63,
    ],
)
def test_person_pseudonym_rejects_anything_but_hex_hmac(db: Session, pid: str) -> None:
    db.add(PersonPseudonym(pid=pid, key_kind=PersonKeyKind.ORCID))
    with pytest.raises(IntegrityError, match="ck_person_pseudonym_pid"):
        db.flush()


def test_person_pseudonym_accepts_hex_hmac(db: Session) -> None:
    pid = hmac.new(b"test-secret", b"0000-0002-1825-0097", hashlib.sha256).hexdigest()
    db.add(PersonPseudonym(pid=pid, key_kind=PersonKeyKind.ORCID))
    db.flush()


# --- ground truth: samples and labels -----------------------------------------------------


def _sample(db: Session, **kw: Any) -> Sample:
    fields: dict[str, Any] = {
        "name": "g1-deadline-test",
        "gate": "G1",
        "frame": "active job postings with a deadline assertion",
        "frame_size": 1200,
        "seed": 20261017,
        "n": 2,
        "drawn_by": "aoi sample@0.1",
    }
    fields.update(kw)
    sample = Sample(**fields)
    db.add(sample)
    db.flush()
    return sample


def _item(db: Session, sample: Sample, ordinal: int = 1, posting: str = "p1") -> SampleItem:
    item = SampleItem(sample_id=sample.id, ordinal=ordinal, item={"posting_id": posting})
    db.add(item)
    db.flush()
    return item


def test_sample_rejects_n_larger_than_frame(db: Session) -> None:
    with pytest.raises(IntegrityError, match="ck_sample_n"):
        _sample(db, frame_size=10, n=11)


def test_sample_item_rejects_same_item_twice(db: Session) -> None:
    sample = _sample(db)
    _item(db, sample, ordinal=1, posting="p1")
    with pytest.raises(IntegrityError, match="uq_sample_item_item"):
        _item(db, sample, ordinal=2, posting="p1")


def test_label_allows_one_current_label_per_item_and_labeller(db: Session) -> None:
    item = _item(db, _sample(db))
    for verdict in (True, False):
        db.add(Label(sample_item_id=item.id, labeller="reviewer", label={"correct": verdict}))
    with pytest.raises(IntegrityError, match="uq_label_current_item_labeller"):
        db.flush()


def test_label_relabel_after_superseding(db: Session) -> None:
    item = _item(db, _sample(db))
    first = Label(sample_item_id=item.id, labeller="reviewer", label={"correct": True})
    db.add(first)
    db.flush()
    first.superseded_at = NOW
    db.add(Label(sample_item_id=item.id, labeller="reviewer", label={"correct": False}))
    db.flush()


# --- sightings (ADR 0033) ---------------------------------------------------------------


def test_a_posting_is_sighted_once_per_fetch(db: Session) -> None:
    source = _source(db)
    posting, fetch = _posting(db, source), _fetch(db, source)
    db.add(PostingSighting(posting_id=posting.id, fetch_id=fetch.id, observed_at=NOW))
    db.flush()
    db.add(PostingSighting(posting_id=posting.id, fetch_id=fetch.id, observed_at=NOW))
    with pytest.raises(IntegrityError, match="uq_posting_sighting_posting_fetch"):
        db.flush()


# --- never delete (ADR 0005, 0027, 0028) --------------------------------------------------

PROTECTED_TABLES = [
    "evidence.fetch",
    "core.assertion",
    "core.posting",
    "core.posting_sighting",
    "core.posting_history",
    "core.position_history",
    "core.position_link",
    "core.org_alias",
    "ops.sample",
    "ops.sample_item",
    "ops.label",
]


def _protected_rows(db: Session) -> dict[str, uuid.UUID]:
    """One row in every protected table, keyed by table name."""
    source = _source(db)
    posting, fetch = _posting(db, source), _fetch(db, source)
    run = _run(db, fetch)
    position = _position(db)
    _org(db)
    item = _item(db, _sample(db))
    rows = {
        "core.assertion": _assertion(posting, fetch, run),
        "core.posting_history": PostingHistory(
            posting_id=posting.id, observed_from=NOW, snapshot={}
        ),
        "core.position_history": PositionHistory(
            position_id=position.id, observed_from=NOW, snapshot={}
        ),
        "core.posting_sighting": PostingSighting(
            posting_id=posting.id, fetch_id=fetch.id, observed_at=NOW
        ),
        "core.position_link": _link(posting, position),
        "core.org_alias": _alias("CERN"),
        "ops.label": Label(sample_item_id=item.id, labeller="reviewer", label={"ok": True}),
    }
    db.add_all(rows.values())
    db.flush()
    ids = {table: row.id for table, row in rows.items()}
    ids |= {
        "evidence.fetch": fetch.id,
        "core.posting": posting.id,
        "ops.sample": item.sample_id,
        "ops.sample_item": item.id,
    }
    assert set(ids) == set(PROTECTED_TABLES)
    return ids


@pytest.mark.parametrize("table", PROTECTED_TABLES)
def test_delete_forbidden_on_protected_table(db: Session, table: str) -> None:
    row_id = _protected_rows(db)[table]
    with pytest.raises(IntegrityError, match="forbidden"):
        db.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})


@pytest.mark.parametrize("table", ["evidence.fetch", "core.assertion", "core.posting_sighting"])
def test_update_forbidden_on_immutable_table(db: Session, table: str) -> None:
    row_id = _protected_rows(db)[table]
    with pytest.raises(IntegrityError, match="forbidden"):
        db.execute(text(f"UPDATE {table} SET created_at = now() WHERE id = :id"), {"id": row_id})


def test_erasure_ref_allows_delete_and_logs_it(db: Session) -> None:
    label_id = _protected_rows(db)["ops.label"]
    db.execute(text("SET LOCAL aoi.erasure_ref = 'GDPR-2026-001'"))
    db.execute(text("DELETE FROM ops.label WHERE id = :id"), {"id": label_id})
    log = db.execute(
        text("SELECT table_name, erasure_ref FROM ops.erasure_log WHERE row_id = :id"),
        {"id": label_id},
    ).one()
    assert tuple(log) == ("ops.label", "GDPR-2026-001")


def test_erasure_ref_does_not_outlive_its_savepoint(db: Session) -> None:
    label_id = _protected_rows(db)["ops.label"]
    nested = db.begin_nested()
    db.execute(text("SET LOCAL aoi.erasure_ref = 'GDPR-2026-001'"))
    nested.rollback()
    # Exercises the guard's '' branch: an ended SET LOCAL reads as '', not NULL.
    assert db.execute(text("SELECT current_setting('aoi.erasure_ref', true)")).scalar() == ""
    with pytest.raises(IntegrityError, match="forbidden"):
        db.execute(text("DELETE FROM ops.label WHERE id = :id"), {"id": label_id})


@pytest.mark.parametrize(
    "statement", ["DELETE FROM ops.erasure_log", "UPDATE ops.erasure_log SET erasure_ref = 'x'"]
)
def test_erasure_log_is_append_only_even_during_erasure(db: Session, statement: str) -> None:
    label_id = _protected_rows(db)["ops.label"]
    db.execute(text("SET LOCAL aoi.erasure_ref = 'GDPR-2026-001'"))
    db.execute(text("DELETE FROM ops.label WHERE id = :id"), {"id": label_id})
    with pytest.raises(IntegrityError, match="forbidden"):
        db.execute(text(statement))


# --- disciplines form a multi-parent graph (overlaps are intended) ------------------------


def _discipline(db: Session, code: str, scheme: DisciplineScheme) -> Discipline:
    discipline = Discipline(code=code, scheme=scheme, label=code)
    db.add(discipline)
    db.flush()
    return discipline


def test_discipline_may_have_several_parents(db: Session) -> None:
    for code in ("fos:1.3", "fos:3.2"):
        _discipline(db, code, DisciplineScheme.OECD_FOS_2007)
    _discipline(db, "arxiv:physics.med-ph", DisciplineScheme.ARXIV)
    for parent in ("fos:1.3", "fos:3.2"):
        db.add(DisciplineParent(child_code="arxiv:physics.med-ph", parent_code=parent))
    db.flush()


def test_discipline_cannot_be_its_own_parent(db: Session) -> None:
    _discipline(db, "fos:1.3", DisciplineScheme.OECD_FOS_2007)
    db.add(DisciplineParent(child_code="fos:1.3", parent_code="fos:1.3"))
    with pytest.raises(IntegrityError, match="ck_discipline_parent_not_self"):
        db.flush()
