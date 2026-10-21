"""provenance_trigger

Revision ID: b83903f66727
Revises: a1762dbcf9e9
Create Date: 2026-09-25 11:57:06.384983

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b83903f66727"
down_revision: str | Sequence[str] | None = "a1762dbcf9e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# All tables that carry an updated_at column.
_TABLES_WITH_UPDATED_AT = [
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
]


def upgrade() -> None:
    """Create updated_at trigger and provenance completeness trigger."""

    # ------------------------------------------------------------------
    # 1. updated_at maintenance trigger function
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    for table in _TABLES_WITH_UPDATED_AT:
        op.execute(f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON "{table}"
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
        """)

    # ------------------------------------------------------------------
    # 2. Provenance completeness trigger on opportunity
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION assert_provenance_complete()
        RETURNS TRIGGER AS $$
        DECLARE
            missing_field TEXT;
        BEGIN
            -- Only enforce for 'verified' state
            IF NEW.publication_state <> 'verified' THEN
                RETURN NEW;
            END IF;

            -- Check each required field has a current assertion with source_document_id
            FOR missing_field IN
                SELECT unnest(ARRAY['title', 'deadline', 'application_url'])
            LOOP
                IF NOT EXISTS (
                    SELECT 1
                    FROM field_assertion fa
                    WHERE fa.opportunity_id = NEW.id
                      AND fa.field_path = missing_field
                      AND fa.is_current = true
                      AND fa.source_document_id IS NOT NULL
                ) THEN
                    RAISE EXCEPTION
                        'opportunity % missing verified assertion: %',
                        NEW.id, missing_field;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE TRIGGER trg_opportunity_provenance
        BEFORE INSERT OR UPDATE ON opportunity
        FOR EACH ROW EXECUTE FUNCTION assert_provenance_complete();
    """)


def downgrade() -> None:
    """Drop updated_at triggers and provenance trigger."""
    # Drop provenance trigger first (depends on function)
    op.execute("DROP TRIGGER IF EXISTS trg_opportunity_provenance ON opportunity;")
    op.execute("DROP FUNCTION IF EXISTS assert_provenance_complete();")

    # Drop updated_at triggers
    for table in _TABLES_WITH_UPDATED_AT:
        op.execute(f'DROP TRIGGER IF EXISTS trg_{table}_updated_at ON "{table}";')
    op.execute("DROP FUNCTION IF EXISTS update_updated_at();")
