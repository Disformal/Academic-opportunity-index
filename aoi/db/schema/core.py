"""core schema: sources, observations (L1) and entities (L2)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aoi.db.schema.base import LayerBase, created_at, pg_enum, pk, updated_at
from aoi.db.schema.enums import (
    AliasMethod,
    AssertionMethod,
    CareerStage,
    ClosureReason,
    ContractType,
    DatePrecision,
    DisciplineScheme,
    LegalBasis,
    LinkMethod,
    OpportunityKind,
    OrgRelationType,
    OrgStatus,
    PersonKeyKind,
    PostingIdentityKind,
    PostingState,
    RequiredDegree,
    RobotsStatus,
    SalaryPeriod,
    SourceKind,
)

SCHEMA = "core"


class Source(LayerBase):
    __tablename__ = "source"
    __table_args__ = (
        UniqueConstraint("name", name="uq_source_name"),
        {"schema": SCHEMA, "comment": "A source of postings and its legal basis (ADR 0012)."},
    )

    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Stable short name, e.g. 'inspire'."
    )
    kind: Mapped[SourceKind] = mapped_column(
        pg_enum(SourceKind, "source_kind", SCHEMA),
        nullable=False,
        comment="api, feed, ats_platform, jsonld, html or oai.",
    )
    platform: Mapped[str | None] = mapped_column(
        String(50), comment="Shared job-board platform (e.g. 'workday'); null if bespoke."
    )
    base_url: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Entry point for discovery."
    )
    legal_basis: Mapped[LegalBasis | None] = mapped_column(
        pg_enum(LegalBasis, "legal_basis", SCHEMA),
        comment="Why we may collect from it; the scheduler refuses null.",
    )
    consent_ref: Mapped[str | None] = mapped_column(
        Text, comment="Pointer to the consent record in docs/sources/, when basis is consent."
    )
    tos_url: Mapped[str | None] = mapped_column(Text, comment="Terms of service URL.")
    robots_status: Mapped[RobotsStatus] = mapped_column(
        pg_enum(RobotsStatus, "robots_status", SCHEMA),
        nullable=False,
        server_default="unknown",
        comment="Result of the last robots.txt assessment.",
    )
    scope_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)),
        nullable=False,
        server_default="{}",
        comment="Free tags for coverage scope, e.g. 'physics', 'eu'.",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", comment="Scheduler only crawls if true."
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class ExtractionRun(LayerBase):
    __tablename__ = "extraction_run"
    __table_args__ = (
        CheckConstraint(
            "(model_id IS NULL) = (prompt_sha256 IS NULL)",
            name="ck_extraction_run_llm_fields",
        ),
        UniqueConstraint(
            "fetch_id", "extractor", "extractor_version", name="uq_extraction_run_fetch_version"
        ),
        {"schema": SCHEMA, "comment": "One execution of one extractor version."},
    )

    id: Mapped[uuid.UUID] = pk()
    fetch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.fetch.id"),
        nullable=False,
        comment="The document extracted; one run per fetch per extractor version (ADR 0030).",
    )
    extractor: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Extractor name, e.g. 'inspire.api'."
    )
    extractor_version: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Version of the extractor code; replay changes it."
    )
    model_id: Mapped[str | None] = mapped_column(
        String(200), comment="LLM model identifier; null for deterministic extractors."
    )
    model_quantisation: Mapped[str | None] = mapped_column(
        String(50), comment="LLM quantisation, e.g. 'Q4_K_M'."
    )
    prompt_sha256: Mapped[str | None] = mapped_column(
        String(64), comment="Hex SHA-256 of the prompt template; required iff model_id is set."
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Start of the run (UTC)."
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="End of the run; null while running or if it crashed."
    )
    created_at: Mapped[datetime] = created_at()


class Posting(LayerBase):
    __tablename__ = "posting"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "identity_kind", "identity_key", name="uq_posting_source_identity"
        ),
        CheckConstraint(
            "(state = 'closed') = (closure_reason IS NOT NULL)",
            name="ck_posting_closure_reason",
        ),
        CheckConstraint("last_seen_at >= first_seen_at", name="ck_posting_seen_order"),
        {"schema": SCHEMA, "comment": "One listing at one source (ADR 0008). Never deleted."},
    )

    id: Mapped[uuid.UUID] = pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.source.id"), nullable=False, comment="Where the listing appears."
    )
    identity_kind: Mapped[PostingIdentityKind] = mapped_column(
        pg_enum(PostingIdentityKind, "posting_identity_kind", SCHEMA),
        nullable=False,
        comment="native_ref (source's own ID) or detail_url (canonical URL).",
    )
    identity_key: Mapped[str] = mapped_column(
        Text, nullable=False, comment="The native ref or canonical detail URL."
    )
    detail_url: Mapped[str | None] = mapped_column(
        Text, comment="Canonical detail page URL, when one exists."
    )
    kind: Mapped[OpportunityKind | None] = mapped_column(
        pg_enum(OpportunityKind, "opportunity_kind", SCHEMA),
        comment="job, funding_call, fellowship or scholarship; null until classified.",
    )
    state: Mapped[PostingState] = mapped_column(
        pg_enum(PostingState, "posting_state", SCHEMA),
        nullable=False,
        server_default="pending",
        comment="pending → active → stale → closed; or removed (404/410 twice).",
    )
    closure_reason: Mapped[ClosureReason | None] = mapped_column(
        pg_enum(ClosureReason, "closure_reason", SCHEMA),
        comment="Why the posting closed; set iff state is closed.",
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Observed time of first sighting."
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Observed time of latest sighting."
    )
    disappeared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="First observed absence from the source; proxy for time-to-fill.",
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Assertion(LayerBase):
    __tablename__ = "assertion"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_assertion_confidence"),
        CheckConstraint(
            # Both-or-neither: a CHECK that evaluates to NULL passes, so test NOT NULL explicitly.
            "span_start IS NULL AND span_end IS NULL "
            "OR span_start IS NOT NULL AND span_end IS NOT NULL "
            "AND span_start >= 0 AND span_end > span_start",
            name="ck_assertion_span",
        ),
        CheckConstraint(
            "method IN ('official_api', 'structured_markup') AND pointer IS NOT NULL "
            "OR method = 'template' AND span_start IS NOT NULL "
            "OR method = 'llm' AND quote IS NOT NULL",
            name="ck_assertion_evidence_pointer",
        ),
        Index("ix_assertion_posting_field", "posting_id", "field"),
        {
            "schema": SCHEMA,
            "comment": "One field value from one document (L1). Append-only, never deleted.",
        },
    )

    id: Mapped[uuid.UUID] = pk()
    posting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.posting.id"), nullable=False, comment="The posting this value describes."
    )
    fetch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.fetch.id"),
        nullable=False,
        comment="The fetched document the value was read from.",
    )
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.extraction_run.id"),
        nullable=False,
        comment="The extractor execution that produced it.",
    )
    field: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Field name, e.g. 'title', 'deadline'."
    )
    value: Mapped[Any] = mapped_column(JSONB, nullable=False, comment="Normalised value.")
    raw: Mapped[str | None] = mapped_column(Text, comment="Value as it appeared in the document.")
    method: Mapped[AssertionMethod] = mapped_column(
        pg_enum(AssertionMethod, "assertion_method", SCHEMA),
        nullable=False,
        comment="How it was read; decides which evidence pointer is required.",
    )
    pointer: Mapped[str | None] = mapped_column(
        Text, comment="JSON Pointer (RFC 6901) or XPath into the document; for API and markup."
    )
    span_start: Mapped[int | None] = mapped_column(
        Integer, comment="Start character offset in the page text; for template extraction."
    )
    span_end: Mapped[int | None] = mapped_column(Integer, comment="End offset (exclusive).")
    quote: Mapped[str | None] = mapped_column(
        Text, comment="Verbatim supporting text from the page; required for LLM extraction."
    )
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, comment="Extractor confidence in [0, 1]."
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Observed time of the document."
    )
    created_at: Mapped[datetime] = created_at()


class PostingSighting(LayerBase):
    __tablename__ = "posting_sighting"
    __table_args__ = (
        UniqueConstraint("posting_id", "fetch_id", name="uq_posting_sighting_posting_fetch"),
        Index("ix_posting_sighting_posting_observed", "posting_id", "observed_at"),
        {
            "schema": SCHEMA,
            "comment": "A fetch that showed a posting (ADR 0033). Append-only, never deleted.",
        },
    )

    id: Mapped[uuid.UUID] = pk()
    posting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.posting.id"), nullable=False, comment="The posting shown."
    )
    fetch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.fetch.id"),
        nullable=False,
        comment="The fetch that showed it; for an unchanged fetch, its earlier content did.",
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Observed time: when the fetch was made."
    )
    created_at: Mapped[datetime] = created_at()


# --- reference entities: organizations (ROR mirror, ADR 0009) and disciplines --------------


class Organization(LayerBase):
    __tablename__ = "organization"
    __table_args__ = (
        CheckConstraint("ror_id ~ '^0[a-z0-9]{6}[0-9]{2}$'", name="ck_organization_ror_id"),
        {"schema": SCHEMA, "comment": "Mirror of the ROR registry, one row per ROR record."},
    )

    ror_id: Mapped[str] = mapped_column(
        String(9), primary_key=True, comment="Bare ROR ID, e.g. '01ggx4157' (no URL prefix)."
    )
    display_name: Mapped[str] = mapped_column(
        Text, nullable=False, comment="The ROR display name."
    )
    names: Mapped[Any] = mapped_column(
        JSONB,
        nullable=False,
        comment="All ROR names: [{value, types, lang}] (labels, aliases, acronyms).",
    )
    types: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)),
        nullable=False,
        comment="ROR types, e.g. education, facility, funder, government.",
    )
    status: Mapped[OrgStatus] = mapped_column(
        pg_enum(OrgStatus, "org_status", SCHEMA),
        nullable=False,
        comment="active, inactive or withdrawn.",
    )
    country_code: Mapped[str | None] = mapped_column(
        String(2), comment="ISO 3166-1 alpha-2 of the first ROR location."
    )
    city: Mapped[str | None] = mapped_column(Text, comment="GeoNames name of the first location.")
    lat: Mapped[float | None] = mapped_column(Double, comment="Latitude of the first location.")
    lon: Mapped[float | None] = mapped_column(Double, comment="Longitude of the first location.")
    established: Mapped[int | None] = mapped_column(SmallInteger, comment="Year founded.")
    fundref_ids: Mapped[list[str]] = mapped_column(
        ARRAY(String(30)),
        nullable=False,
        server_default="{}",
        comment="Crossref Funder IDs listed in ROR; the join key to funder data.",
    )
    ror_release: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="ROR dump release this row was loaded from."
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class OrganizationRelation(LayerBase):
    __tablename__ = "organization_relation"
    __table_args__ = (
        PrimaryKeyConstraint("ror_id", "related_ror_id", "type"),
        {"schema": SCHEMA, "comment": "ROR relationships between organizations."},
    )

    ror_id: Mapped[str] = mapped_column(
        ForeignKey("core.organization.ror_id"), comment="The organization the relation is on."
    )
    related_ror_id: Mapped[str] = mapped_column(
        ForeignKey("core.organization.ror_id"), comment="The related organization."
    )
    type: Mapped[OrgRelationType] = mapped_column(
        pg_enum(OrgRelationType, "org_relation_type", SCHEMA),
        comment="parent, child, related, successor or predecessor (as seen from ror_id).",
    )


class OrgAlias(LayerBase):
    __tablename__ = "org_alias"
    __table_args__ = (
        CheckConstraint(
            "method = 'human' OR confidence IS NOT NULL", name="ck_org_alias_confidence"
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_org_alias_range"),
        Index(
            "uq_org_alias_current_raw",
            "raw",
            unique=True,
            postgresql_where="superseded_at IS NULL",
        ),
        {
            "schema": SCHEMA,
            "comment": "Raw organization string -> ROR. Superseded, never overwritten.",
        },
    )

    id: Mapped[uuid.UUID] = pk()
    raw: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Organization string exactly as a source wrote it."
    )
    ror_id: Mapped[str | None] = mapped_column(
        ForeignKey("core.organization.ror_id"),
        comment="Matched organization; null = checked, no confident match.",
    )
    method: Mapped[AliasMethod] = mapped_column(
        pg_enum(AliasMethod, "alias_method", SCHEMA),
        nullable=False,
        comment="ror_api (affiliation matching), rule or human.",
    )
    confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), comment="Match score in [0, 1]; required unless method is human."
    )
    decided_by: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Matcher name and version, or the human reviewer."
    )
    created_at: Mapped[datetime] = created_at()
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When a newer mapping replaced this one."
    )


class Discipline(LayerBase):
    __tablename__ = "discipline"
    __table_args__ = (
        {"schema": SCHEMA, "comment": "Taxonomy: OECD FOS 2007 plus arXiv physics categories."},
    )

    code: Mapped[str] = mapped_column(
        String(40),
        primary_key=True,
        comment="Scheme-prefixed code, e.g. 'fos:1.3', 'arxiv:hep-th'.",
    )
    scheme: Mapped[DisciplineScheme] = mapped_column(
        pg_enum(DisciplineScheme, "discipline_scheme", SCHEMA),
        nullable=False,
        comment="oecd_fos_2007 or arxiv.",
    )
    label: Mapped[str] = mapped_column(Text, nullable=False, comment="English label.")


class DisciplineParent(LayerBase):
    __tablename__ = "discipline_parent"
    __table_args__ = (
        PrimaryKeyConstraint("child_code", "parent_code"),
        CheckConstraint("child_code <> parent_code", name="ck_discipline_parent_not_self"),
        {
            "schema": SCHEMA,
            "comment": "Broader-narrower links. A discipline may have several parents: "
            "overlaps between fields are intended.",
        },
    )

    child_code: Mapped[str] = mapped_column(
        ForeignKey("core.discipline.code"), comment="The narrower discipline."
    )
    parent_code: Mapped[str] = mapped_column(
        ForeignKey("core.discipline.code"),
        comment="One of its broader disciplines, e.g. an arXiv category's FOS field.",
    )


# --- positions, links and history (ADR 0008) ----------------------------------------------


class Position(LayerBase):
    __tablename__ = "position"
    __table_args__ = (
        # Target of the details tables' (position_id, kind) foreign keys.
        UniqueConstraint("id", "kind", name="uq_position_id_kind"),
        CheckConstraint(
            "(deadline_at IS NULL) = (deadline_precision IS NULL)",
            name="ck_position_deadline_precision",
        ),
        {"schema": SCHEMA, "comment": "The real-world opportunity behind one or more postings."},
    )

    id: Mapped[uuid.UUID] = pk()
    kind: Mapped[OpportunityKind] = mapped_column(
        pg_enum(OpportunityKind, "opportunity_kind", SCHEMA),
        nullable=False,
        comment="job, funding_call, fellowship or scholarship.",
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, comment="Resolved title.")
    ror_id: Mapped[str | None] = mapped_column(
        ForeignKey("core.organization.ror_id"), comment="Resolved organization."
    )
    country_code: Mapped[str | None] = mapped_column(
        String(2), comment="ISO 3166-1 alpha-2 where the position is based."
    )
    city: Mapped[str | None] = mapped_column(Text, comment="City where the position is based.")
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Source-stated publication date (valid time)."
    )
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Application deadline (valid time); null = none stated."
    )
    deadline_precision: Mapped[DatePrecision | None] = mapped_column(
        pg_enum(DatePrecision, "date_precision", SCHEMA),
        comment="day, month or year; set iff deadline_at is set.",
    )
    application_url: Mapped[str | None] = mapped_column(Text, comment="Where to apply.")
    in_scope: Mapped[bool | None] = mapped_column(
        Boolean, comment="Within the site's coverage (e.g. physics); null until classified."
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class PositionDiscipline(LayerBase):
    __tablename__ = "position_discipline"
    __table_args__ = (
        PrimaryKeyConstraint("position_id", "discipline_code"),
        {"schema": SCHEMA, "comment": "Disciplines a position belongs to."},
    )

    position_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.position.id"), comment="The position."
    )
    discipline_code: Mapped[str] = mapped_column(
        ForeignKey("core.discipline.code"), comment="One of its disciplines."
    )


class PositionLink(LayerBase):
    __tablename__ = "position_link"
    __table_args__ = (
        CheckConstraint(
            "method = 'human' OR probability IS NOT NULL", name="ck_position_link_probability"
        ),
        CheckConstraint(
            "probability >= 0 AND probability <= 1", name="ck_position_link_probability_range"
        ),
        Index(
            "uq_position_link_current_posting",
            "posting_id",
            unique=True,
            postgresql_where="superseded_at IS NULL",
        ),
        Index("ix_position_link_position", "position_id"),
        {
            "schema": SCHEMA,
            "comment": "posting -> position. Superseded, never deleted (ADR 0008).",
        },
    )

    id: Mapped[uuid.UUID] = pk()
    posting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.posting.id"), nullable=False, comment="The posting being linked."
    )
    position_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.position.id"), nullable=False, comment="The position it is linked to."
    )
    method: Mapped[LinkMethod] = mapped_column(
        pg_enum(LinkMethod, "link_method", SCHEMA),
        nullable=False,
        comment="auto (linker) or human (review queue).",
    )
    probability: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), comment="Linker match probability in [0, 1]; required for auto links."
    )
    signals: Mapped[Any | None] = mapped_column(
        JSONB, comment="Comparison values that produced the probability."
    )
    decided_by: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Linker name and version, or the human reviewer."
    )
    created_at: Mapped[datetime] = created_at()
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When a newer decision replaced this link."
    )


class PostingHistory(LayerBase):
    __tablename__ = "posting_history"
    __table_args__ = (
        CheckConstraint("observed_to > observed_from", name="ck_posting_history_order"),
        Index(
            "uq_posting_history_current",
            "posting_id",
            unique=True,
            postgresql_where="observed_to IS NULL",
        ),
        {"schema": SCHEMA, "comment": "Resolved posting fields over observed time."},
    )

    id: Mapped[uuid.UUID] = pk()
    posting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.posting.id"), nullable=False, comment="The posting."
    )
    observed_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Observed time we first saw this version.",
    )
    observed_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="Observed time a newer version replaced it; null = current.",
    )
    stated_posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Source-stated publication date (valid time)."
    )
    stated_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Source-stated deadline (valid time)."
    )
    snapshot: Mapped[Any] = mapped_column(
        JSONB, nullable=False, comment="All resolved fields of this version."
    )


class PositionHistory(LayerBase):
    __tablename__ = "position_history"
    __table_args__ = (
        CheckConstraint("observed_to > observed_from", name="ck_position_history_order"),
        Index(
            "uq_position_history_current",
            "position_id",
            unique=True,
            postgresql_where="observed_to IS NULL",
        ),
        {"schema": SCHEMA, "comment": "Resolved position fields over observed time."},
    )

    id: Mapped[uuid.UUID] = pk()
    position_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.position.id"), nullable=False, comment="The position."
    )
    observed_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Observed time this version was first resolved.",
    )
    observed_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="Observed time a newer version replaced it; null = current.",
    )
    snapshot: Mapped[Any] = mapped_column(
        JSONB, nullable=False, comment="All resolved fields of this version."
    )


# --- kind-specific details -----------------------------------------------------------------


class _ResearcherTerms:
    """Columns shared by job_details and fellowship_details."""

    career_stage: Mapped[CareerStage | None] = mapped_column(
        pg_enum(CareerStage, "career_stage", SCHEMA), comment="R1-R4 (EU research careers)."
    )
    required_degree: Mapped[RequiredDegree | None] = mapped_column(
        pg_enum(RequiredDegree, "required_degree", SCHEMA),
        comment="Minimum degree required to apply.",
    )
    max_years_since_phd: Mapped[int | None] = mapped_column(
        SmallInteger, comment="Eligibility limit in years after the PhD."
    )
    required_languages: Mapped[list[str]] = mapped_column(
        ARRAY(String(3)),
        nullable=False,
        server_default="{}",
        comment="ISO 639 codes of languages required for the role.",
    )
    duration_months: Mapped[int | None] = mapped_column(
        SmallInteger, comment="Length of the appointment or award."
    )
    start_date: Mapped[date | None] = mapped_column(Date, comment="Stated start date.")
    salary_min: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), comment="Lower bound of pay or stipend, as stated."
    )
    salary_max: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), comment="Upper bound of pay or stipend, as stated."
    )
    salary_currency: Mapped[str | None] = mapped_column(
        String(3), comment="ISO 4217 code of the stated pay."
    )
    salary_period: Mapped[SalaryPeriod | None] = mapped_column(
        pg_enum(SalaryPeriod, "salary_period", SCHEMA), comment="year, month or hour."
    )
    salary_eur_annual: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), comment="Midpoint pay normalised to EUR per year (ECB reference rate)."
    )
    fx_date: Mapped[date | None] = mapped_column(
        Date, comment="Date of the ECB rate used for salary_eur_annual."
    )


def _pay_checks(table: str) -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint("salary_max >= salary_min", name=f"ck_{table}_salary_order"),
        CheckConstraint(
            "salary_min IS NULL AND salary_max IS NULL "
            "OR salary_currency IS NOT NULL AND salary_period IS NOT NULL",
            name=f"ck_{table}_salary_unit",
        ),
        CheckConstraint(
            "salary_eur_annual IS NULL OR fx_date IS NOT NULL", name=f"ck_{table}_salary_fx"
        ),
    )


class JobDetails(_ResearcherTerms, LayerBase):
    __tablename__ = "job_details"
    __table_args__ = (
        ForeignKeyConstraint(["position_id", "kind"], ["core.position.id", "core.position.kind"]),
        CheckConstraint("kind = 'job'", name="ck_job_details_kind"),
        CheckConstraint("fte > 0 AND fte <= 1", name="ck_job_details_fte"),
        *_pay_checks("job_details"),
        {"schema": SCHEMA, "comment": "Fields specific to jobs."},
    )

    position_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, comment="The position.")
    kind: Mapped[OpportunityKind] = mapped_column(
        pg_enum(OpportunityKind, "opportunity_kind", SCHEMA),
        nullable=False,
        server_default="job",
        comment="Always 'job'; part of the foreign key that enforces it.",
    )
    contract_type: Mapped[ContractType | None] = mapped_column(
        pg_enum(ContractType, "contract_type", SCHEMA),
        comment="permanent, fixed_term or tenure_track.",
    )
    fte: Mapped[Decimal | None] = mapped_column(
        Numeric(3, 2), comment="Full-time equivalent in (0, 1]."
    )


class FellowshipDetails(_ResearcherTerms, LayerBase):
    __tablename__ = "fellowship_details"
    __table_args__ = (
        ForeignKeyConstraint(["position_id", "kind"], ["core.position.id", "core.position.kind"]),
        CheckConstraint(
            "kind IN ('fellowship', 'scholarship')", name="ck_fellowship_details_kind"
        ),
        *_pay_checks("fellowship_details"),
        {"schema": SCHEMA, "comment": "Fields specific to fellowships and scholarships."},
    )

    position_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, comment="The position.")
    kind: Mapped[OpportunityKind] = mapped_column(
        pg_enum(OpportunityKind, "opportunity_kind", SCHEMA),
        nullable=False,
        comment="fellowship or scholarship; part of the foreign key that enforces it.",
    )


class CallDetails(LayerBase):
    __tablename__ = "call_details"
    __table_args__ = (
        ForeignKeyConstraint(["position_id", "kind"], ["core.position.id", "core.position.kind"]),
        CheckConstraint("kind = 'funding_call'", name="ck_call_details_kind"),
        CheckConstraint("funding_max >= funding_min", name="ck_call_details_funding_order"),
        CheckConstraint(
            "funding_min IS NULL AND funding_max IS NULL OR funding_currency IS NOT NULL",
            name="ck_call_details_funding_currency",
        ),
        {"schema": SCHEMA, "comment": "Fields specific to funding calls."},
    )

    position_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, comment="The position.")
    kind: Mapped[OpportunityKind] = mapped_column(
        pg_enum(OpportunityKind, "opportunity_kind", SCHEMA),
        nullable=False,
        server_default="funding_call",
        comment="Always 'funding_call'; part of the foreign key that enforces it.",
    )
    funder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.funder.id"), comment="Who funds the call."
    )
    call_identifier: Mapped[str | None] = mapped_column(
        Text, comment="The funder's call code, e.g. 'ERC-2027-StG'."
    )
    opens_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When applications open (valid time)."
    )
    funding_min: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 2), comment="Lower bound of funding per award, as stated."
    )
    funding_max: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 2), comment="Upper bound of funding per award, as stated."
    )
    funding_currency: Mapped[str | None] = mapped_column(
        String(3), comment="ISO 4217 code of the stated funding."
    )
    duration_months: Mapped[int | None] = mapped_column(
        SmallInteger, comment="Length of the funded project."
    )


# --- money and people: schema now, data after launch (ADR 0010, 0011) ----------------------


class Funder(LayerBase):
    __tablename__ = "funder"
    __table_args__ = (
        UniqueConstraint("ror_id", name="uq_funder_ror_id"),
        UniqueConstraint("crossref_funder_id", name="uq_funder_crossref_id"),
        CheckConstraint(
            "ror_id IS NOT NULL OR crossref_funder_id IS NOT NULL", name="ck_funder_has_key"
        ),
        {"schema": SCHEMA, "comment": "A funding body, keyed by ROR and/or Crossref Funder ID."},
    )

    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(Text, nullable=False, comment="Funder name.")
    ror_id: Mapped[str | None] = mapped_column(
        ForeignKey("core.organization.ror_id"), comment="The funder's ROR record."
    )
    crossref_funder_id: Mapped[str | None] = mapped_column(
        String(30), comment="Crossref Funder Registry ID (e.g. '501100000781' for the ERC)."
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Award(LayerBase):
    __tablename__ = "award"
    __table_args__ = (
        UniqueConstraint("source", "native_id", name="uq_award_source_native_id"),
        CheckConstraint("end_date >= start_date", name="ck_award_dates"),
        CheckConstraint("amount IS NULL OR currency IS NOT NULL", name="ck_award_amount_currency"),
        {"schema": SCHEMA, "comment": "A funded project from an award database."},
    )

    id: Mapped[uuid.UUID] = pk()
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Award database, e.g. 'cordis', 'nih_reporter'."
    )
    native_id: Mapped[str] = mapped_column(
        Text, nullable=False, comment="The award's ID in that database."
    )
    funder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.funder.id"), comment="Who funded it."
    )
    ror_id: Mapped[str | None] = mapped_column(
        ForeignKey("core.organization.ror_id"), comment="Host organization."
    )
    title: Mapped[str | None] = mapped_column(Text, comment="Project title.")
    amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), comment="Awarded amount.")
    currency: Mapped[str | None] = mapped_column(String(3), comment="ISO 4217 code of amount.")
    start_date: Mapped[date | None] = mapped_column(Date, comment="Project start.")
    end_date: Mapped[date | None] = mapped_column(Date, comment="Project end.")
    discipline_code: Mapped[str | None] = mapped_column(
        ForeignKey("core.discipline.code"), comment="Main discipline."
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class GrantReference(LayerBase):
    __tablename__ = "grant_reference"
    __table_args__ = (
        {"schema": SCHEMA, "comment": "A grant number found in a posting, raw and normalised."},
    )

    id: Mapped[uuid.UUID] = pk()
    assertion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.assertion.id"),
        nullable=False,
        comment="The assertion the grant number was extracted in (provenance).",
    )
    raw: Mapped[str] = mapped_column(Text, nullable=False, comment="As written in the posting.")
    normalised: Mapped[str | None] = mapped_column(
        Text, comment="Canonical form per funder rules, e.g. '101042368'."
    )
    funder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.funder.id"), comment="Funder the number belongs to, once known."
    )
    award_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.award.id"), comment="Matched award, once award data is ingested."
    )
    created_at: Mapped[datetime] = created_at()


class PersonPseudonym(LayerBase):
    __tablename__ = "person_pseudonym"
    __table_args__ = (
        CheckConstraint("pid ~ '^[0-9a-f]{64}$'", name="ck_person_pseudonym_pid"),
        {
            "schema": SCHEMA,
            "comment": "A countable person with no identifying data (ADR 0011).",
        },
    )

    pid: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="Hex HMAC-SHA256(secret, ORCID or source person key). Never the key itself.",
    )
    key_kind: Mapped[PersonKeyKind] = mapped_column(
        pg_enum(PersonKeyKind, "person_key_kind", SCHEMA),
        nullable=False,
        comment="What was hashed: orcid or source_key.",
    )
    created_at: Mapped[datetime] = created_at()


class AwardPerson(LayerBase):
    __tablename__ = "award_person"
    __table_args__ = (
        PrimaryKeyConstraint("award_id", "pid", "role"),
        {"schema": SCHEMA, "comment": "People on an award, as pseudonyms only."},
    )

    award_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.award.id"), comment="Award.")
    pid: Mapped[str] = mapped_column(
        ForeignKey("core.person_pseudonym.pid"), comment="Pseudonymised person."
    )
    role: Mapped[str] = mapped_column(
        String(50), comment="Role as the award database states it, e.g. 'pi', 'co_pi'."
    )
