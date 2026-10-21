"""m2_task_host

Host politeness (plan §4.3): ops.task.host_id, so a claim can require the host's next_allowed_at
to have passed and move it forward in the same transaction.

Revision ID: 07a311ccbe27
Revises: 2d538c946b70
Create Date: 2026-10-18 12:20:36.496615

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "07a311ccbe27"
down_revision: str | Sequence[str] | None = "2d538c946b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ops.task.host_id with its foreign key to ops.host."""
    op.add_column(
        "task",
        sa.Column(
            "host_id",
            sa.Uuid(),
            nullable=True,
            comment="Host the task contacts; claimable only once the host is due.",
        ),
        schema="ops",
    )
    op.create_foreign_key(
        "task_host_id_fkey",
        "task",
        "host",
        ["host_id"],
        ["id"],
        source_schema="ops",
        referent_schema="ops",
    )


def downgrade() -> None:
    """Drop the column and its foreign key."""
    op.drop_constraint("task_host_id_fkey", "task", schema="ops", type_="foreignkey")
    op.drop_column("task", "host_id", schema="ops")
