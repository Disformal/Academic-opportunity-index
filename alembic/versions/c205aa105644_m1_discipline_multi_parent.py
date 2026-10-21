"""m1_discipline_multi_parent

Disciplines form a graph, not a tree: core.discipline.parent_code (one parent) is
replaced by core.discipline_parent (several parents per discipline). Overlaps between
fields are intended (decided 2026-10-17), e.g. medical physics under both physics
and clinical medicine. The table is empty when this runs (no taxonomy seeded yet).

Revision ID: c205aa105644
Revises: 2c407579e5ee
Create Date: 2026-10-17 14:46:11.900268

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c205aa105644"
down_revision: str | Sequence[str] | None = "2c407579e5ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace the single parent column with the discipline_parent link table."""

    op.create_table(
        "discipline_parent",
        sa.Column(
            "child_code", sa.String(length=40), nullable=False, comment="The narrower discipline."
        ),
        sa.Column(
            "parent_code",
            sa.String(length=40),
            nullable=False,
            comment="One of its broader disciplines, e.g. an arXiv category's FOS field.",
        ),
        sa.CheckConstraint("child_code <> parent_code", name="ck_discipline_parent_not_self"),
        sa.ForeignKeyConstraint(
            ["child_code"],
            ["core.discipline.code"],
        ),
        sa.ForeignKeyConstraint(
            ["parent_code"],
            ["core.discipline.code"],
        ),
        sa.PrimaryKeyConstraint("child_code", "parent_code"),
        schema="core",
        comment="Broader-narrower links. A discipline may have several parents: "
        "overlaps between fields are intended.",
    )
    op.drop_column("discipline", "parent_code", schema="core")


def downgrade() -> None:
    """Restore the single parent column (keeps no links: a tree cannot hold them all)."""
    op.add_column(
        "discipline",
        sa.Column(
            "parent_code",
            sa.String(40),
            sa.ForeignKey("core.discipline.code"),
            nullable=True,
            comment="Broader discipline; arXiv categories point to their FOS field.",
        ),
        schema="core",
    )
    op.drop_table("discipline_parent", schema="core")
