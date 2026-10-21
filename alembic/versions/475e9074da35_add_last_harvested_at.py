"""add_last_harvested_at

Revision ID: 475e9074da35
Revises: 3869bb814e73
Create Date: 2026-09-26 18:52:49.825603

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "475e9074da35"
down_revision: str | Sequence[str] | None = "3869bb814e73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add last_harvested_at timestamp to source table."""
    op.add_column(
        "source",
        sa.Column("last_harvested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Remove last_harvested_at from source table."""
    op.drop_column("source", "last_harvested_at")
