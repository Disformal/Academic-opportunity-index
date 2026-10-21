"""SQLAlchemy 2.0 declarative models for the AOI database."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import text

from aoi.db.enums import (
    ClosureReason,
    Derivation,
    FetchOutcome,
    IdentityKind,
    OpportunityStatus,
    PageRole,
    PublicationState,
)


def pg_enum(py_enum: type, name: str) -> SAEnum:
    """Create a PostgreSQL enum that stores the value (lowercase string), not the name."""
    return SAEnum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


def pk() -> Mapped[uuid.UUID]:
    """Standard UUID primary key with auto-generated default."""
    return mapped_column(primary_key=True, default=uuid.uuid4)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 1. Source
# ---------------------------------------------------------------------------


class Source(Base):
    __tablename__ = "source"
    __table_args__ = (
        CheckConstraint("trust_tier >= 1 AND trust_tier <= 5", name="ck_source_tier"),
        UniqueConstraint("name", name="uq_source_name"),
    )

    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    first_party: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    trust_tier: Mapped[int] = mapped_column(SmallInteger, default=3, server_default="3")
    countries: Mapped[list[str]] = mapped_column(
        ARRAY(String(2)), default=list, server_default="{}"
    )
    languages: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), default=list, server_default="{}"
    )
    acquisition_decision: Mapped[str] = mapped_column(
        String(30), default="pending", server_default="pending"
    )
    requests_per_second: Mapped[Any] = mapped_column(
        Numeric(6, 3), default=0.5, server_default="0.5"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_harvested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    oai_pmh_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    crawl_urls: Mapped[list["CrawlUrl"]] = relationship("CrawlUrl", back_populates="source")
    robots_assessments: Mapped[list["RobotsAssessment"]] = relationship(
        "RobotsAssessment", back_populates="source"
    )


# ---------------------------------------------------------------------------
# 2. CrawlUrl
# ---------------------------------------------------------------------------


class CrawlUrl(Base):
    __tablename__ = "crawl_url"
    __table_args__ = (
        UniqueConstraint("source_id", "url_hash", name="uq_crawlurl"),
        Index("ix_crawlurl_parent", "parent_listing_id"),
    )

    id: Mapped[uuid.UUID] = pk()
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source.id"), nullable=False)
    url_canonical: Mapped[str] = mapped_column(Text, nullable=False)
    url_original: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    page_role: Mapped[PageRole] = mapped_column(pg_enum(PageRole, "page_role"), nullable=False)
    parent_listing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("crawl_url.id"), nullable=True
    )
    consecutive_absences: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    etag: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    source: Mapped["Source"] = relationship("Source", back_populates="crawl_urls")
    fetches: Mapped[list["Fetch"]] = relationship("Fetch", back_populates="crawl_url")


# ---------------------------------------------------------------------------
# 3. Fetch
# ---------------------------------------------------------------------------


class Fetch(Base):
    __tablename__ = "fetch"
    __table_args__ = (Index("ix_fetch_source_time", "source_id", "fetched_at"),)

    id: Mapped[uuid.UUID] = pk()
    crawl_url_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crawl_url.id"), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source.id"), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    http_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    outcome: Mapped[FetchOutcome] = mapped_column(
        pg_enum(FetchOutcome, "fetch_outcome"), nullable=False
    )
    content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_prefix: Mapped[str | None] = mapped_column(String(300), nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    crawl_url: Mapped["CrawlUrl"] = relationship("CrawlUrl", back_populates="fetches")
    source_document: Mapped["SourceDocument | None"] = relationship(
        "SourceDocument", back_populates="fetch", uselist=False
    )


# ---------------------------------------------------------------------------
# 4. SourceDocument
# ---------------------------------------------------------------------------


class SourceDocument(Base):
    __tablename__ = "source_document"

    id: Mapped[uuid.UUID] = pk()
    fetch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fetch.id"), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source.id"), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    detected_language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    text_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    fetch: Mapped["Fetch"] = relationship("Fetch", back_populates="source_document")
    extraction_runs: Mapped[list["ExtractionRun"]] = relationship(
        "ExtractionRun", back_populates="source_document"
    )


# ---------------------------------------------------------------------------
# 5. ExtractionRun
# ---------------------------------------------------------------------------


class ExtractionRun(Base):
    __tablename__ = "extraction_run"

    id: Mapped[uuid.UUID] = pk()
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_document.id"), nullable=False
    )
    extractor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(50), nullable=False)
    taxonomy_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    items_emitted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    source_document: Mapped["SourceDocument"] = relationship(
        "SourceDocument", back_populates="extraction_runs"
    )
    field_assertions: Mapped[list["FieldAssertion"]] = relationship(
        "FieldAssertion", back_populates="extraction_run"
    )


# ---------------------------------------------------------------------------
# 6. Opportunity
# ---------------------------------------------------------------------------


class Opportunity(Base):
    __tablename__ = "opportunity"
    __table_args__ = (
        UniqueConstraint("identity_kind", "identity_key", name="uq_identity"),
        CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)",
            name="ck_closed_consistent",
        ),
        Index(
            "ix_opp_published",
            "publication_state",
            "deadline",
        ),
    )

    id: Mapped[uuid.UUID] = pk()
    identity_kind: Mapped[IdentityKind] = mapped_column(
        pg_enum(IdentityKind, "identity_kind"), nullable=False
    )
    identity_key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[OpportunityStatus] = mapped_column(
        pg_enum(OpportunityStatus, "opportunity_status"), nullable=False
    )
    publication_state: Mapped[PublicationState] = mapped_column(
        pg_enum(PublicationState, "publication_state"),
        default=PublicationState.UNVERIFIED,
        server_default="unverified",
        nullable=False,
    )
    primary_language: Mapped[str] = mapped_column(String(10), default="en", server_default="en")
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_precision: Mapped[str | None] = mapped_column(String(10), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    application_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closure_reason: Mapped[ClosureReason | None] = mapped_column(
        pg_enum(ClosureReason, "closure_reason"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    search_vector: Mapped[Any] = mapped_column(TSVECTOR, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    field_assertions: Mapped[list["FieldAssertion"]] = relationship(
        "FieldAssertion", back_populates="opportunity"
    )
    versions: Mapped[list["OpportunityVersion"]] = relationship(
        "OpportunityVersion", back_populates="opportunity"
    )


# ---------------------------------------------------------------------------
# 7. FieldAssertion
# ---------------------------------------------------------------------------


class FieldAssertion(Base):
    __tablename__ = "field_assertion"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_conf"),
        CheckConstraint(
            "text_end IS NULL OR text_start IS NULL OR text_end > text_start",
            name="ck_span",
        ),
        Index(
            "ix_fa_current",
            "opportunity_id",
            "field_path",
            postgresql_where=text("is_current = true"),
        ),
    )

    id: Mapped[uuid.UUID] = pk()
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("opportunity.id"), nullable=False)
    field_path: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Any] = mapped_column(Numeric(4, 3), nullable=False)
    derivation: Mapped[Derivation] = mapped_column(
        pg_enum(Derivation, "derivation"), nullable=False
    )
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_document.id"), nullable=False
    )
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction_run.id"), nullable=False
    )
    text_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    json_pointer: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    opportunity: Mapped["Opportunity"] = relationship(
        "Opportunity", back_populates="field_assertions"
    )
    extraction_run: Mapped["ExtractionRun"] = relationship(
        "ExtractionRun", back_populates="field_assertions"
    )


# ---------------------------------------------------------------------------
# 8. OpportunityVersion
# ---------------------------------------------------------------------------


class OpportunityVersion(Base):
    __tablename__ = "opportunity_version"

    id: Mapped[uuid.UUID] = pk()
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("opportunity.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    change_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    caused_by_extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_run.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    opportunity: Mapped["Opportunity"] = relationship("Opportunity", back_populates="versions")


# ---------------------------------------------------------------------------
# 9. DuplicateCluster
# ---------------------------------------------------------------------------


class DuplicateCluster(Base):
    __tablename__ = "duplicate_cluster"

    id: Mapped[uuid.UUID] = pk()
    canonical_opportunity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opportunity.id"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[Any] = mapped_column(Numeric(5, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    members: Mapped[list["DuplicateMember"]] = relationship(
        "DuplicateMember", back_populates="cluster"
    )


# ---------------------------------------------------------------------------
# 10. DuplicateMember
# ---------------------------------------------------------------------------


class DuplicateMember(Base):
    __tablename__ = "duplicate_member"
    __table_args__ = (PrimaryKeyConstraint("cluster_id", "opportunity_id"),)

    cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("duplicate_cluster.id"), nullable=False
    )
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("opportunity.id"), nullable=False)
    probability: Mapped[Any] = mapped_column(Numeric(5, 4), nullable=False)
    signals: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    cluster: Mapped["DuplicateCluster"] = relationship(
        "DuplicateCluster", back_populates="members"
    )


# ---------------------------------------------------------------------------
# 11. RobotsAssessment
# ---------------------------------------------------------------------------


class RobotsAssessment(Base):
    __tablename__ = "robots_assessment"

    id: Mapped[uuid.UUID] = pk()
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source.id"), nullable=False)
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    robots_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    terms_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(String(30), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    assessed_by: Mapped[str] = mapped_column(String(50), nullable=False)
    review_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    source: Mapped["Source"] = relationship("Source", back_populates="robots_assessments")


# ---------------------------------------------------------------------------
# 12. Organization
# ---------------------------------------------------------------------------


class PipelineRun(Base):
    __tablename__ = "pipeline_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="ck_pipeline_run_status",
        ),
        CheckConstraint(
            "triggered_by IN ('cli', 'api', 'cron')",
            name="ck_pipeline_run_triggered_by",
        ),
        Index("ix_pipeline_run_source_time", "source_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = pk()
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="running")
    created: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    unchanged: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    closed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    quarantined: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(30), nullable=False, server_default="cli")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    # Relationships
    source: Mapped["Source"] = relationship("Source")


# ---------------------------------------------------------------------------
# 12. Organization
# ---------------------------------------------------------------------------


class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[uuid.UUID] = pk()
    ror_id: Mapped[str | None] = mapped_column(String(50), nullable=True, unique=True)
    preferred_name: Mapped[str] = mapped_column(String(500), nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    org_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
