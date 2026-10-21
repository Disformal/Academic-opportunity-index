"""m1_no_delete_guards

Database-enforced "never delete" (ADR 0005, 0027, 0028):
- ops.guard_delete(): BEFORE DELETE on the protected tables. Raises restrict_violation
  unless the transaction set aoi.erasure_ref (legal erasure only); then the deletion is
  recorded in ops.erasure_log (table, primary key, ticket; never the row content).
- ops.forbid_change(): raises unconditionally. BEFORE UPDATE on the immutable L0/L1 rows
  (evidence.fetch, core.assertion); BEFORE UPDATE OR DELETE on ops.erasure_log.
TRUNCATE and DROP TABLE are not row operations and are not caught.

Revision ID: 2c407579e5ee
Revises: 05187c633648
Create Date: 2026-10-17 14:03:20.904730

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2c407579e5ee"
down_revision: str | Sequence[str] | None = "05187c633648"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every table here has a uuid primary key named id (logged by ops.guard_delete).
_NO_DELETE = [
    "evidence.fetch",
    "core.assertion",
    "core.posting",
    "core.posting_history",
    "core.position_history",
    "core.position_link",
    "core.org_alias",
    "ops.sample",
    "ops.sample_item",
    "ops.label",
]
_NO_UPDATE = ["evidence.fetch", "core.assertion"]


def _name(table: str) -> str:
    return table.split(".")[1]


def upgrade() -> None:
    """Create the erasure log, the guard functions and their triggers."""

    op.create_table(
        "erasure_log",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "table_name",
            sa.Text(),
            nullable=False,
            comment="Schema-qualified table the row was deleted from.",
        ),
        sa.Column(
            "row_id",
            sa.Uuid(),
            nullable=False,
            comment="Primary key of the deleted row (its content is not kept).",
        ),
        sa.Column(
            "erasure_ref",
            sa.Text(),
            nullable=False,
            comment="Ticket of the erasure request, from aoi.erasure_ref.",
        ),
        sa.Column(
            "erased_by",
            sa.Text(),
            server_default=sa.text("CURRENT_USER"),
            nullable=False,
            comment="Database role that ran the delete.",
        ),
        sa.Column(
            "erased_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When the row was deleted.",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="ops",
        comment="Rows deleted under a legal erasure (ADR 0027). Append-only (ADR 0028).",
    )

    op.execute("""
        CREATE FUNCTION ops.guard_delete()
        RETURNS TRIGGER AS $$
        DECLARE
            ref TEXT := current_setting('aoi.erasure_ref', true);
        BEGIN
            -- NULL if never set in this session; '' once a SET LOCAL has ended.
            IF ref IS NULL OR ref = '' THEN
                RAISE EXCEPTION
                    'DELETE on %.% is forbidden (ADR 0027); erasure needs aoi.erasure_ref',
                    TG_TABLE_SCHEMA, TG_TABLE_NAME
                    USING ERRCODE = 'restrict_violation';
            END IF;
            INSERT INTO ops.erasure_log (table_name, row_id, erasure_ref)
            VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, OLD.id, ref);
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE FUNCTION ops.forbid_change()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION '% on %.% is forbidden (ADR 0027, 0028)',
                TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
    """)

    for table in _NO_DELETE:
        op.execute(f"""
            CREATE TRIGGER trg_{_name(table)}_no_delete
            BEFORE DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION ops.guard_delete();
        """)
    for table in _NO_UPDATE:
        op.execute(f"""
            CREATE TRIGGER trg_{_name(table)}_no_update
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION ops.forbid_change();
        """)
    op.execute("""
        CREATE TRIGGER trg_erasure_log_append_only
        BEFORE UPDATE OR DELETE ON ops.erasure_log
        FOR EACH ROW EXECUTE FUNCTION ops.forbid_change();
    """)


def downgrade() -> None:
    """Drop the triggers, functions and the erasure log."""
    op.execute("DROP TRIGGER IF EXISTS trg_erasure_log_append_only ON ops.erasure_log")
    for table in _NO_UPDATE:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{_name(table)}_no_update ON {table}")
    for table in _NO_DELETE:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{_name(table)}_no_delete ON {table}")
    op.execute("DROP FUNCTION IF EXISTS ops.forbid_change()")
    op.execute("DROP FUNCTION IF EXISTS ops.guard_delete()")
    op.drop_table("erasure_log", schema="ops")
