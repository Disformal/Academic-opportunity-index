"""m2_task_open_uniqueness

ops.task.crawl_target_id (a fetch task's target, previously in the payload) and two partial unique
indexes: at most one open (queued, running or failed) task per kind and target, and at most one
open discover task per source (ADR 0030 §2).

Revision ID: 4f9333c8f24b
Revises: 43db414f940f
Create Date: 2026-10-18 15:58:03.549851

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f9333c8f24b"
down_revision: str | Sequence[str] | None = "43db414f940f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN = "state IN ('queued', 'running', 'failed')"


def upgrade() -> None:
    """Add crawl_target_id and the two open-task uniqueness indexes."""
    op.add_column(
        "task",
        sa.Column(
            "crawl_target_id",
            sa.Uuid(),
            nullable=True,
            comment="Target a fetch task fetches; at most one open task per kind and target.",
        ),
        schema="ops",
    )
    op.create_foreign_key(
        "task_crawl_target_id_fkey",
        "task",
        "crawl_target",
        ["crawl_target_id"],
        ["id"],
        source_schema="ops",
        referent_schema="ops",
    )
    op.create_index(
        "uq_task_open_target",
        "task",
        ["kind", "crawl_target_id"],
        unique=True,
        schema="ops",
        postgresql_where=f"crawl_target_id IS NOT NULL AND {_OPEN}",
    )
    op.create_index(
        "uq_task_open_discover",
        "task",
        ["source_id"],
        unique=True,
        schema="ops",
        postgresql_where=f"kind = 'discover' AND {_OPEN}",
    )


def downgrade() -> None:
    """Reverse the upgrade."""
    op.drop_index("uq_task_open_discover", table_name="task", schema="ops")
    op.drop_index("uq_task_open_target", table_name="task", schema="ops")
    op.drop_constraint("task_crawl_target_id_fkey", "task", schema="ops", type_="foreignkey")
    op.drop_column("task", "crawl_target_id", schema="ops")
