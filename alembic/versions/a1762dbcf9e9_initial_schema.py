"""initial_schema

Revision ID: a1762dbcf9e9
Revises:
Create Date: 2026-09-25 11:56:12.109023

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1762dbcf9e9"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "opportunity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "identity_kind",
            sa.Enum(
                "native_ref",
                "application_url",
                "detail_url",
                "fingerprint",
                name="identity_kind",
            ),
            nullable=False,
        ),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("type", sa.String(length=60), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "upcoming",
                "open",
                "rolling",
                "closed",
                "unknown",
                name="opportunity_status",
            ),
            nullable=False,
        ),
        sa.Column(
            "publication_state",
            sa.Enum(
                "unverified",
                "provisional",
                "verified",
                "disputed",
                "rejected",
                name="publication_state",
            ),
            server_default="unverified",
            nullable=False,
        ),
        sa.Column("primary_language", sa.String(length=10), server_default="en", nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_precision", sa.String(length=10), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("application_url", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "closure_reason",
            sa.Enum(
                "removed_404",
                "delisted",
                "stated_closed",
                "deadline_passed",
                "superseded",
                "operator",
                name="closure_reason",
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)", name="ck_closed_consistent"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_kind", "identity_key", name="uq_identity"),
    )
    op.create_index(
        "ix_opp_published", "opportunity", ["publication_state", "deadline"], unique=False
    )
    op.create_table(
        "organization",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ror_id", sa.String(length=50), nullable=True),
        sa.Column("preferred_name", sa.String(length=500), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("org_type", sa.String(length=50), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ror_id"),
    )
    op.create_table(
        "source",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("first_party", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("trust_tier", sa.SmallInteger(), server_default="3", nullable=False),
        sa.Column(
            "countries",
            postgresql.ARRAY(sa.String(length=2)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "languages",
            postgresql.ARRAY(sa.String(length=10)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "acquisition_decision",
            sa.String(length=30),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "requests_per_second",
            sa.Numeric(precision=6, scale=3),
            server_default="0.5",
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("trust_tier >= 1 AND trust_tier <= 5", name="ck_source_tier"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_source_name"),
    )
    op.create_table(
        "crawl_url",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("url_canonical", sa.Text(), nullable=False),
        sa.Column("url_original", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "page_role",
            sa.Enum("listing", "detail", "unknown", name="page_role"),
            nullable=False,
        ),
        sa.Column("parent_listing_id", sa.Uuid(), nullable=True),
        sa.Column(
            "consecutive_absences",
            sa.SmallInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("etag", sa.String(length=300), nullable=True),
        sa.Column("last_modified", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["parent_listing_id"],
            ["crawl_url.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "url_hash", name="uq_crawlurl"),
    )
    op.create_index("ix_crawlurl_parent", "crawl_url", ["parent_listing_id"], unique=False)
    op.create_table(
        "duplicate_cluster",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("method", sa.String(length=50), nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["canonical_opportunity_id"],
            ["opportunity.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "robots_assessment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("robots_body", sa.Text(), nullable=True),
        sa.Column("terms_url", sa.Text(), nullable=True),
        sa.Column("verdict", sa.String(length=30), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("assessed_by", sa.String(length=50), nullable=False),
        sa.Column("review_due", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "duplicate_member",
        sa.Column("cluster_id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("probability", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["duplicate_cluster.id"],
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["opportunity.id"],
        ),
        sa.PrimaryKeyConstraint("cluster_id", "opportunity_id"),
    )
    op.create_table(
        "fetch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("crawl_url_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(
                "hit",
                "not_modified",
                "error",
                "blocked",
                "rate_limited",
                name="fetch_outcome",
            ),
            nullable=False,
        ),
        sa.Column("content_digest", sa.String(length=64), nullable=True),
        sa.Column("evidence_prefix", sa.String(length=300), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("body_bytes", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["crawl_url_id"],
            ["crawl_url.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fetch_source_time", "fetch", ["source_id", "fetched_at"], unique=False)
    op.create_table(
        "source_document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fetch_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column("detected_language", sa.String(length=10), nullable=True),
        sa.Column("text_object_key", sa.Text(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["fetch_id"],
            ["fetch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "extraction_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("extractor_id", sa.String(length=100), nullable=False),
        sa.Column("extractor_version", sa.String(length=50), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=50), nullable=True),
        sa.Column("input_digest", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=True),
        sa.Column("items_emitted", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["source_document.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "field_assertion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("field_path", sa.String(length=100), nullable=False),
        sa.Column(
            "normalized_value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("raw_value", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column(
            "derivation",
            sa.Enum(
                "official_api",
                "structured_markup",
                "template",
                "parser",
                "human",
                "inherited",
                name="derivation",
            ),
            nullable=False,
        ),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("text_start", sa.Integer(), nullable=True),
        sa.Column("text_end", sa.Integer(), nullable=True),
        sa.Column("json_pointer", sa.String(length=300), nullable=True),
        sa.Column("is_current", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_conf"),
        sa.CheckConstraint(
            "text_end IS NULL OR text_start IS NULL OR text_end > text_start",
            name="ck_span",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_run_id"],
            ["extraction_run.id"],
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["opportunity.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["source_document.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_fa_current",
        "field_assertion",
        ["opportunity_id", "field_path"],
        unique=False,
        postgresql_where=sa.text("is_current = true"),
    )
    op.create_table(
        "opportunity_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("change_reason", sa.String(length=50), nullable=True),
        sa.Column("caused_by_extraction_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["caused_by_extraction_run_id"],
            ["extraction_run.id"],
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["opportunity.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("opportunity_version")
    op.drop_index(
        "ix_fa_current",
        table_name="field_assertion",
        postgresql_where=sa.text("is_current = true"),
    )
    op.drop_table("field_assertion")
    op.drop_table("extraction_run")
    op.drop_table("source_document")
    op.drop_index("ix_fetch_source_time", table_name="fetch")
    op.drop_table("fetch")
    op.drop_table("duplicate_member")
    op.drop_table("robots_assessment")
    op.drop_table("duplicate_cluster")
    op.drop_index("ix_crawlurl_parent", table_name="crawl_url")
    op.drop_table("crawl_url")
    op.drop_table("source")
    op.drop_table("organization")
    op.drop_index("ix_opp_published", table_name="opportunity")
    op.drop_table("opportunity")
    # Drop PostgreSQL enum types created by the upgrade
    sa.Enum(name="derivation").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="fetch_outcome").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="page_role").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="identity_kind").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="opportunity_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="publication_state").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="closure_reason").drop(op.get_bind(), checkfirst=True)
