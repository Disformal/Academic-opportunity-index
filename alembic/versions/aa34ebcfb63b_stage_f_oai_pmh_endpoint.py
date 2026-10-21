"""stage_f_oai_pmh_endpoint

Revision ID: aa34ebcfb63b
Revises: e37b7267086f
Create Date: 2026-10-05 00:42:58.580424

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa34ebcfb63b"
down_revision: str | Sequence[str] | None = "e37b7267086f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add oai_pmh_endpoint column to source table."""
    op.add_column("source", sa.Column("oai_pmh_endpoint", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove oai_pmh_endpoint column from source table."""
    op.drop_column("source", "oai_pmh_endpoint")
