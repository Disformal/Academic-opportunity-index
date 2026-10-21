"""stage_d_last_verified_at

Revision ID: 68b335304b2a
Revises: 475e9074da35
Create Date: 2026-10-05 00:22:41.962679

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "68b335304b2a"
down_revision: str | Sequence[str] | None = "475e9074da35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add last_verified_at to field_assertion.

    This column records the last time a conditional GET confirmed the source
    URL still returned the same content (HTTP 304 Not Modified). It lets the
    closure detector distinguish "stale assertion" from "recently verified but
    not changed".
    """
    op.add_column(
        "field_assertion",
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Remove last_verified_at from field_assertion."""
    op.drop_column("field_assertion", "last_verified_at")
