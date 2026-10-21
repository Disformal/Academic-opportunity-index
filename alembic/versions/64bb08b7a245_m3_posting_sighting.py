"""m3_posting_sighting

Sightings (ADR 0033): core.posting_sighting records each fetch that showed a posting, one row per
posting and fetch. Like the other observation tables it is append-only: the ADR 0027 guards reject
DELETE (except through the logged erasure path) and UPDATE.

Revision ID: 64bb08b7a245
Revises: 2d29e0bf85ea
Create Date: 2026-10-20 15:28:47.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "64bb08b7a245"
down_revision: str | Sequence[str] | None = "2d29e0bf85ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table, its index and its no-delete and no-update guards."""
    op.create_table(
        "posting_sighting",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "posting_id",
            sa.Uuid(),
            sa.ForeignKey("core.posting.id"),
            nullable=False,
            comment="The posting shown.",
        ),
        sa.Column(
            "fetch_id",
            sa.Uuid(),
            sa.ForeignKey("evidence.fetch.id"),
            nullable=False,
            comment="The fetch that showed it; for an unchanged fetch, its earlier content did.",
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Observed time: when the fetch was made.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("posting_id", "fetch_id", name="uq_posting_sighting_posting_fetch"),
        schema="core",
        comment="A fetch that showed a posting (ADR 0033). Append-only, never deleted.",
    )
    op.create_index(
        "ix_posting_sighting_posting_observed",
        "posting_sighting",
        ["posting_id", "observed_at"],
        schema="core",
    )
    op.execute("""
        CREATE TRIGGER trg_posting_sighting_no_delete
        BEFORE DELETE ON core.posting_sighting
        FOR EACH ROW EXECUTE FUNCTION ops.guard_delete();
    """)
    op.execute("""
        CREATE TRIGGER trg_posting_sighting_no_update
        BEFORE UPDATE ON core.posting_sighting
        FOR EACH ROW EXECUTE FUNCTION ops.forbid_change();
    """)


def downgrade() -> None:
    """Reverse the upgrade."""
    op.execute("DROP TRIGGER IF EXISTS trg_posting_sighting_no_update ON core.posting_sighting")
    op.execute("DROP TRIGGER IF EXISTS trg_posting_sighting_no_delete ON core.posting_sighting")
    op.drop_index(
        "ix_posting_sighting_posting_observed", table_name="posting_sighting", schema="core"
    )
    op.drop_table("posting_sighting", schema="core")
