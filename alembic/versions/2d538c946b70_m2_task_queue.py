"""m2_task_queue

The work queue (plan §4.3, ADR 0014, 0030): ops.task, claimed by workers under a lease with
FOR UPDATE SKIP LOCKED.

Revision ID: 2d538c946b70
Revises: c205aa105644
Create Date: 2026-10-18 11:52:23.022287

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2d538c946b70"
down_revision: str | Sequence[str] | None = "c205aa105644"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ops.task with its state enum, claim index and updated_at trigger."""
    op.create_table(
        "task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            comment="Handler that runs it: discover, fetch, extract, …",
        ),
        sa.Column("source_id", sa.Uuid(), nullable=False, comment="Source the work belongs to."),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default="{}",
            nullable=False,
            comment="Handler input, e.g. a target id.",
        ),
        sa.Column(
            "state",
            sa.Enum(
                "queued", "running", "done", "failed", "dead", name="task_state", schema="ops"
            ),
            server_default="queued",
            nullable=False,
            comment="queued, running, done, failed (retry at run_after) or dead (gave up).",
        ),
        sa.Column(
            "priority",
            sa.SmallInteger(),
            server_default="0",
            nullable=False,
            comment="Higher is claimed first.",
        ),
        sa.Column(
            "run_after",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Not claimable before this time (the retry backoff).",
        ),
        sa.Column(
            "attempts",
            sa.SmallInteger(),
            server_default="0",
            nullable=False,
            comment="Claims so far; counted at claim time, so crashes count too.",
        ),
        sa.Column(
            "locked_by",
            sa.String(length=100),
            nullable=True,
            comment="Worker holding the lease, e.g. 'host:pid'.",
        ),
        sa.Column(
            "locked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the lease was taken.",
        ),
        sa.Column(
            "last_error",
            sa.Text(),
            nullable=True,
            comment="Error class and message of the last failed attempt.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_task_attempts"),
        sa.CheckConstraint("(locked_by IS NULL) = (locked_at IS NULL)", name="ck_task_lock_pair"),
        sa.CheckConstraint(
            "state <> 'running' OR locked_by IS NOT NULL", name="ck_task_running_locked"
        ),
        sa.ForeignKeyConstraint(["source_id"], ["core.source.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="ops",
        comment="The work queue (ADR 0014, 0030). Workers claim under a lease.",
    )
    op.create_index(
        "ix_task_claimable",
        "task",
        ["kind", "run_after"],
        schema="ops",
        postgresql_where="state IN ('queued', 'failed')",
    )
    # updated_at maintenance (function created by b83903f66727)
    op.execute("""
        CREATE TRIGGER trg_task_updated_at
        BEFORE UPDATE ON ops.task
        FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();
    """)


def downgrade() -> None:
    """Drop the trigger, table and enum created above."""
    op.execute("DROP TRIGGER IF EXISTS trg_task_updated_at ON ops.task")
    op.drop_index("ix_task_claimable", table_name="task", schema="ops")
    op.drop_table("task", schema="ops")
    op.execute("DROP TYPE IF EXISTS ops.task_state")
