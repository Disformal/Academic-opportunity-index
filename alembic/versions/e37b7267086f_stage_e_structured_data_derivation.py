"""stage_e_structured_data_derivation

Add 'structured_data' to the derivation PostgreSQL enum.
Used by structured_extract.py for schema.org/JobPosting JSON-LD fields.

Revision ID: e37b7267086f
Revises: 68b335304b2a
Create Date: 2026-10-05

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e37b7267086f"
down_revision: str | Sequence[str] | None = "68b335304b2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add 'structured_data' value to the derivation enum.

    ALTER TYPE ... ADD VALUE must run outside a transaction on PostgreSQL <12.
    PostgreSQL 16 allows it inside a transaction, but autocommit_block() is the
    correct Alembic pattern — it commits the current transaction, runs the DDL,
    then resumes a new transaction.
    """
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE derivation ADD VALUE IF NOT EXISTS 'structured_data'")


def downgrade() -> None:
    """PostgreSQL does not support removing enum values.

    A no-op is the only viable option for a live database.  If rollback is
    genuinely required, create a new migration that drops affected columns,
    rebuilds the enum without 'structured_data', and recreates the columns.
    """
    pass
