"""m2_task_retry_of

Requeue after review (plan §4.3): ops.task.retry_of links a fresh task to the dead task it retries.
The dead row is never changed; the unique constraint allows at most one retry per dead task.

Revision ID: 2d29e0bf85ea
Revises: 69deed331996
Create Date: 2026-10-20 14:47:59.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2d29e0bf85ea"
down_revision: str | Sequence[str] | None = "69deed331996"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add retry_of, its self-referencing foreign key, and one retry per dead task."""
    op.add_column(
        "task",
        sa.Column(
            "retry_of",
            sa.Uuid(),
            nullable=True,
            comment="Dead task this one retries after review (aoi task requeue); one retry each.",
        ),
        schema="ops",
    )
    op.create_foreign_key(
        "task_retry_of_fkey",
        "task",
        "task",
        ["retry_of"],
        ["id"],
        source_schema="ops",
        referent_schema="ops",
    )
    op.create_unique_constraint("uq_task_retry_of", "task", ["retry_of"], schema="ops")


def downgrade() -> None:
    """Reverse the upgrade."""
    op.drop_constraint("uq_task_retry_of", "task", schema="ops", type_="unique")
    op.drop_constraint("task_retry_of_fkey", "task", schema="ops", type_="foreignkey")
    op.drop_column("task", "retry_of", schema="ops")
