"""m3_crawl_target_retired_at

Retired crawl targets (M3): ops.crawl_target.retired_at. A retired target keeps its row and its
fetches (ADR 0005); the scheduler never queues it and lifecycle passes ignore it (ADR 0033).

Revision ID: ff6e6f4fa217
Revises: 64bb08b7a245
Create Date: 2026-10-20 16:28:16.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ff6e6f4fa217"
down_revision: str | Sequence[str] | None = "64bb08b7a245"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add retired_at."""
    op.add_column(
        "crawl_target",
        sa.Column(
            "retired_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When it was retired: never fetched again, ignored by lifecycle passes; kept.",
        ),
        schema="ops",
    )


def downgrade() -> None:
    """Reverse the upgrade."""
    op.drop_column("crawl_target", "retired_at", schema="ops")
