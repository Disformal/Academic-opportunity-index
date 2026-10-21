"""m1_l0_l1_tables

L0 and L1 of ADR 0007 plus what they reference: core.source, ops.host,
ops.crawl_target, evidence.fetch, core.extraction_run, core.posting, core.assertion.

The provenance rule of b83903f66727 is ported in two parts:
- CHECK constraints on core.assertion: each value carries the evidence pointer its
  method requires (pointer for API/markup, span for template, quote for LLM).
- core.assert_posting_provenance(): a posting may only be active when it has
  title and application_url assertions.

Revision ID: 58e91fc1e994
Revises: a145a3f6a110
Create Date: 2026-10-17 13:52:10.118204

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "58e91fc1e994"
down_revision: str | Sequence[str] | None = "a145a3f6a110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (schema, name) of every enum type created here, for the downgrade.
_ENUMS = [
    ("core", "source_kind"),
    ("core", "legal_basis"),
    ("core", "robots_status"),
    ("ops", "circuit_state"),
    ("ops", "target_role"),
    ("evidence", "fetch_outcome"),
    ("core", "opportunity_kind"),
    ("core", "posting_identity_kind"),
    ("core", "posting_state"),
    ("core", "closure_reason"),
    ("core", "assertion_method"),
]

_TABLES_WITH_UPDATED_AT = ["core.source", "ops.host", "ops.crawl_target", "core.posting"]


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    """Create the L0/L1 tables, their enums and triggers."""
    op.create_table(
        "source",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "name", sa.String(200), nullable=False, comment="Stable short name, e.g. 'inspire'."
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "api",
                "feed",
                "ats_platform",
                "jsonld",
                "html",
                "oai",
                name="source_kind",
                schema="core",
            ),
            nullable=False,
            comment="api, feed, ats_platform, jsonld, html or oai.",
        ),
        sa.Column(
            "platform",
            sa.String(50),
            nullable=True,
            comment="Shared job-board platform (e.g. 'workday'); null if bespoke.",
        ),
        sa.Column("base_url", sa.Text(), nullable=False, comment="Entry point for discovery."),
        sa.Column(
            "legal_basis",
            sa.Enum(
                "official_api",
                "open_licence",
                "primary_institution",
                "consent",
                name="legal_basis",
                schema="core",
            ),
            nullable=True,
            comment="Why we may collect from it; the scheduler refuses null.",
        ),
        sa.Column(
            "consent_ref",
            sa.Text(),
            nullable=True,
            comment="Pointer to the consent record in docs/sources/, when basis is consent.",
        ),
        sa.Column("tos_url", sa.Text(), nullable=True, comment="Terms of service URL."),
        sa.Column(
            "robots_status",
            sa.Enum(
                "allowed",
                "disallowed",
                "not_applicable",
                "unknown",
                name="robots_status",
                schema="core",
            ),
            server_default="unknown",
            nullable=False,
            comment="Result of the last robots.txt assessment.",
        ),
        sa.Column(
            "scope_tags",
            postgresql.ARRAY(sa.String(50)),
            server_default="{}",
            nullable=False,
            comment="Free tags for coverage scope, e.g. 'physics', 'eu'.",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default="false",
            nullable=False,
            comment="Scheduler only crawls if true.",
        ),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("name", name="uq_source_name"),
        schema="core",
        comment="A source of postings and its legal basis (ADR 0012).",
    )

    op.create_table(
        "host",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "hostname", sa.String(253), nullable=False, comment="Lower-case hostname, no port."
        ),
        sa.Column(
            "rps",
            sa.Numeric(6, 3),
            server_default="0.5",
            nullable=False,
            comment="Maximum requests per second to this host.",
        ),
        sa.Column(
            "next_allowed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Earliest time the next request to this host may start.",
        ),
        sa.Column(
            "circuit_state",
            sa.Enum("closed", "open", "half_open", name="circuit_state", schema="ops"),
            server_default="closed",
            nullable=False,
            comment="closed = requests flow; open = paused after failures; half_open = probing.",
        ),
        sa.Column(
            "consecutive_failures",
            sa.SmallInteger(),
            server_default="0",
            nullable=False,
            comment="Failures since the last success; drives the circuit breaker.",
        ),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("hostname", name="uq_host_hostname"),
        sa.CheckConstraint("rps > 0", name="ck_host_rps_positive"),
        schema="ops",
        comment="Per-host politeness and circuit-breaker state.",
    )

    op.create_table(
        "crawl_target",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Uuid(),
            sa.ForeignKey("core.source.id"),
            nullable=False,
            comment="The source this URL belongs to.",
        ),
        sa.Column(
            "host_id",
            sa.Uuid(),
            sa.ForeignKey("ops.host.id"),
            nullable=False,
            comment="Host for politeness accounting.",
        ),
        sa.Column("url", sa.Text(), nullable=False, comment="Canonical URL."),
        sa.Column(
            "url_sha256",
            sa.String(64),
            nullable=False,
            comment="Hex SHA-256 of `url`; the uniqueness key.",
        ),
        sa.Column(
            "role",
            sa.Enum("listing", "detail", "feed", name="target_role", schema="ops"),
            nullable=False,
            comment="listing, detail or feed.",
        ),
        sa.Column(
            "etag",
            sa.String(300),
            nullable=True,
            comment="ETag from the last successful response, for conditional GET.",
        ),
        sa.Column(
            "last_modified",
            sa.String(100),
            nullable=True,
            comment="Last-Modified header from the last successful response, verbatim.",
        ),
        sa.Column(
            "content_sha256",
            sa.String(64),
            nullable=True,
            comment="Hex SHA-256 of the last body; extraction runs only when it changes.",
        ),
        sa.Column(
            "recrawl_interval",
            sa.Interval(),
            server_default=sa.text("interval '1 day'"),
            nullable=False,
            comment="Adaptive: halves on change, doubles otherwise, within [1 h, 7 d].",
        ),
        sa.Column(
            "next_due_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When the scheduler should next enqueue a fetch.",
        ),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("source_id", "url_sha256", name="uq_crawl_target_source_url"),
        sa.CheckConstraint(
            "recrawl_interval BETWEEN interval '1 hour' AND interval '7 days'",
            name="ck_crawl_target_recrawl_bounds",
        ),
        schema="ops",
        comment="A URL we revisit on an adaptive schedule.",
    )

    op.create_table(
        "fetch",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "crawl_target_id",
            sa.Uuid(),
            sa.ForeignKey("ops.crawl_target.id"),
            nullable=False,
            comment="The target that was fetched.",
        ),
        sa.Column("url", sa.Text(), nullable=False, comment="URL requested."),
        sa.Column("final_url", sa.Text(), nullable=True, comment="URL after redirects."),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the request started (UTC).",
        ),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True, comment="Total time to response."),
        sa.Column(
            "outcome",
            sa.Enum(
                "hit",
                "not_modified",
                "error",
                "blocked",
                "rate_limited",
                name="fetch_outcome",
                schema="evidence",
            ),
            nullable=False,
            comment="hit, not_modified, error, blocked (SSRF / robots) or rate_limited.",
        ),
        sa.Column(
            "http_status",
            sa.SmallInteger(),
            nullable=True,
            comment="Final HTTP status; null when no response was received.",
        ),
        sa.Column(
            "resolved_ip",
            postgresql.INET(),
            nullable=True,
            comment="Address the final hop connected to.",
        ),
        sa.Column("body_sha256", sa.String(64), nullable=True, comment="Hex SHA-256 of the body."),
        sa.Column("body_bytes", sa.Integer(), nullable=True, comment="Body size in bytes."),
        sa.Column(
            "warc_record_id",
            sa.String(100),
            nullable=True,
            comment="WARC-Record-ID of the stored response; null until the M2 writer.",
        ),
        sa.Column("error", sa.Text(), nullable=True, comment="Error class and message, if any."),
        _created_at(),
        schema="evidence",
        comment="Every outbound request, successful or not. Immutable.",
    )
    op.create_index(
        "ix_fetch_target_time",
        "fetch",
        ["crawl_target_id", "requested_at"],
        schema="evidence",
    )

    op.create_table(
        "extraction_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "extractor",
            sa.String(100),
            nullable=False,
            comment="Extractor name, e.g. 'inspire.api'.",
        ),
        sa.Column(
            "extractor_version",
            sa.String(50),
            nullable=False,
            comment="Version of the extractor code; replay changes it.",
        ),
        sa.Column(
            "model_id",
            sa.String(200),
            nullable=True,
            comment="LLM model identifier; null for deterministic extractors.",
        ),
        sa.Column(
            "model_quantisation",
            sa.String(50),
            nullable=True,
            comment="LLM quantisation, e.g. 'Q4_K_M'.",
        ),
        sa.Column(
            "prompt_sha256",
            sa.String(64),
            nullable=True,
            comment="Hex SHA-256 of the prompt template; required iff model_id is set.",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Start of the run (UTC).",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="End of the run; null while running or if it crashed.",
        ),
        _created_at(),
        sa.CheckConstraint(
            "(model_id IS NULL) = (prompt_sha256 IS NULL)",
            name="ck_extraction_run_llm_fields",
        ),
        schema="core",
        comment="One execution of one extractor version.",
    )

    op.create_table(
        "posting",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Uuid(),
            sa.ForeignKey("core.source.id"),
            nullable=False,
            comment="Where the listing appears.",
        ),
        sa.Column(
            "identity_kind",
            sa.Enum("native_ref", "detail_url", name="posting_identity_kind", schema="core"),
            nullable=False,
            comment="native_ref (source's own ID) or detail_url (canonical URL).",
        ),
        sa.Column(
            "identity_key",
            sa.Text(),
            nullable=False,
            comment="The native ref or canonical detail URL.",
        ),
        sa.Column(
            "detail_url",
            sa.Text(),
            nullable=True,
            comment="Canonical detail page URL, when one exists.",
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "job",
                "funding_call",
                "fellowship",
                "scholarship",
                name="opportunity_kind",
                schema="core",
            ),
            nullable=True,
            comment="job, funding_call, fellowship or scholarship; null until classified.",
        ),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "active",
                "stale",
                "closed",
                "removed",
                name="posting_state",
                schema="core",
            ),
            server_default="pending",
            nullable=False,
            comment="pending → active → stale → closed; or removed (404/410 twice).",
        ),
        sa.Column(
            "closure_reason",
            sa.Enum(
                "delisted",
                "stated_closed",
                "deadline_passed",
                "operator",
                name="closure_reason",
                schema="core",
            ),
            nullable=True,
            comment="Why the posting closed; set iff state is closed.",
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Observed time of first sighting.",
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Observed time of latest sighting.",
        ),
        sa.Column(
            "disappeared_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="First observed absence from the source; proxy for time-to-fill.",
        ),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "source_id", "identity_kind", "identity_key", name="uq_posting_source_identity"
        ),
        sa.CheckConstraint(
            "(state = 'closed') = (closure_reason IS NOT NULL)",
            name="ck_posting_closure_reason",
        ),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name="ck_posting_seen_order"),
        schema="core",
        comment="One listing at one source (ADR 0008). Never deleted.",
    )

    op.create_table(
        "assertion",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "posting_id",
            sa.Uuid(),
            sa.ForeignKey("core.posting.id"),
            nullable=False,
            comment="The posting this value describes.",
        ),
        sa.Column(
            "fetch_id",
            sa.Uuid(),
            sa.ForeignKey("evidence.fetch.id"),
            nullable=False,
            comment="The fetched document the value was read from.",
        ),
        sa.Column(
            "extraction_run_id",
            sa.Uuid(),
            sa.ForeignKey("core.extraction_run.id"),
            nullable=False,
            comment="The extractor execution that produced it.",
        ),
        sa.Column(
            "field",
            sa.String(100),
            nullable=False,
            comment="Field name, e.g. 'title', 'deadline'.",
        ),
        sa.Column("value", postgresql.JSONB(), nullable=False, comment="Normalised value."),
        sa.Column(
            "raw", sa.Text(), nullable=True, comment="Value as it appeared in the document."
        ),
        sa.Column(
            "method",
            sa.Enum(
                "official_api",
                "structured_markup",
                "template",
                "llm",
                name="assertion_method",
                schema="core",
            ),
            nullable=False,
            comment="How it was read; decides which evidence pointer is required.",
        ),
        sa.Column(
            "pointer",
            sa.Text(),
            nullable=True,
            comment="JSON Pointer (RFC 6901) or XPath into the document; for API and markup.",
        ),
        sa.Column(
            "span_start",
            sa.Integer(),
            nullable=True,
            comment="Start character offset in the page text; for template extraction.",
        ),
        sa.Column("span_end", sa.Integer(), nullable=True, comment="End offset (exclusive)."),
        sa.Column(
            "quote",
            sa.Text(),
            nullable=True,
            comment="Verbatim supporting text from the page; required for LLM extraction.",
        ),
        sa.Column(
            "confidence",
            sa.Numeric(4, 3),
            nullable=False,
            comment="Extractor confidence in [0, 1].",
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Observed time of the document.",
        ),
        _created_at(),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_assertion_confidence"),
        sa.CheckConstraint(
            "span_start IS NULL AND span_end IS NULL "
            "OR span_start IS NOT NULL AND span_end IS NOT NULL "
            "AND span_start >= 0 AND span_end > span_start",
            name="ck_assertion_span",
        ),
        sa.CheckConstraint(
            "method IN ('official_api', 'structured_markup') AND pointer IS NOT NULL "
            "OR method = 'template' AND span_start IS NOT NULL "
            "OR method = 'llm' AND quote IS NOT NULL",
            name="ck_assertion_evidence_pointer",
        ),
        schema="core",
        comment="One field value from one document (L1). Append-only, never deleted.",
    )
    op.create_index(
        "ix_assertion_posting_field", "assertion", ["posting_id", "field"], schema="core"
    )

    # updated_at maintenance (function created by b83903f66727)
    for table in _TABLES_WITH_UPDATED_AT:
        name = table.split(".")[1]
        op.execute(f"""
            CREATE TRIGGER trg_{name}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();
        """)

    # Provenance: a posting is active only with title and application_url assertions.
    op.execute("""
        CREATE OR REPLACE FUNCTION core.assert_posting_provenance()
        RETURNS TRIGGER AS $$
        DECLARE
            required_field TEXT;
        BEGIN
            IF NEW.state <> 'active' THEN
                RETURN NEW;
            END IF;

            FOREACH required_field IN ARRAY ARRAY['title', 'application_url']
            LOOP
                IF NOT EXISTS (
                    SELECT 1
                    FROM core.assertion a
                    WHERE a.posting_id = NEW.id
                      AND a.field = required_field
                ) THEN
                    RAISE EXCEPTION
                        'posting % cannot be active without a % assertion',
                        NEW.id, required_field
                        USING ERRCODE = 'check_violation';
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_posting_provenance
        BEFORE INSERT OR UPDATE OF state ON core.posting
        FOR EACH ROW EXECUTE FUNCTION core.assert_posting_provenance();
    """)


def downgrade() -> None:
    """Drop the triggers, tables and enums created above."""
    op.execute("DROP TRIGGER IF EXISTS trg_posting_provenance ON core.posting")
    op.execute("DROP FUNCTION IF EXISTS core.assert_posting_provenance()")
    for table in _TABLES_WITH_UPDATED_AT:
        name = table.split(".")[1]
        op.execute(f"DROP TRIGGER IF EXISTS trg_{name}_updated_at ON {table}")

    op.drop_index("ix_assertion_posting_field", table_name="assertion", schema="core")
    op.drop_table("assertion", schema="core")
    op.drop_table("posting", schema="core")
    op.drop_table("extraction_run", schema="core")
    op.drop_index("ix_fetch_target_time", table_name="fetch", schema="evidence")
    op.drop_table("fetch", schema="evidence")
    op.drop_table("crawl_target", schema="ops")
    op.drop_table("host", schema="ops")
    op.drop_table("source", schema="core")

    for schema, name in _ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {schema}.{name}")
