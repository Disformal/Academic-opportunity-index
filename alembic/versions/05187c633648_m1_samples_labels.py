"""m1_samples_labels

Ground truth (plan §4.2): ops.sample (frame, frame size, seed, n, strata), ops.sample_item
(drawn items in draw order) and ops.label (one current label per item per labeller;
relabels supersede).

Revision ID: 05187c633648
Revises: 3ad4633a21b5
Create Date: 2026-10-17 13:56:34.039865

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "05187c633648"
down_revision: str | Sequence[str] | None = "3ad4633a21b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the sample, sample item and label tables."""

    op.create_table(
        "sample",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "name",
            sa.String(length=100),
            nullable=False,
            comment="Unique name, e.g. 'g1-deadline-2026-11'.",
        ),
        sa.Column(
            "gate",
            sa.String(length=10),
            nullable=True,
            comment="Launch gate this sample measures (G1-G6), if any.",
        ),
        sa.Column(
            "frame",
            sa.Text(),
            nullable=False,
            comment="Definition of the population sampled from (SQL or prose).",
        ),
        sa.Column(
            "frame_size",
            sa.Integer(),
            nullable=False,
            comment="Population size N when the sample was drawn.",
        ),
        sa.Column(
            "seed",
            sa.BigInteger(),
            nullable=False,
            comment="Random seed; with frame, reproduces the draw.",
        ),
        sa.Column("n", sa.Integer(), nullable=False, comment="Number of items drawn."),
        sa.Column(
            "strata",
            postgresql.JSONB(),
            nullable=True,
            comment="Per stratum: {name: {frame_size, n}}; null for a simple random sample.",
        ),
        sa.Column(
            "drawn_by",
            sa.String(length=100),
            nullable=False,
            comment="Who or what drew the sample (tool and version).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("n > 0 AND n <= frame_size", name="ck_sample_n"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_sample_name"),
        schema="ops",
        comment="A reproducible random sample drawn for labelling.",
    )
    op.create_table(
        "sample_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sample_id", sa.Uuid(), nullable=False, comment="The sample."),
        sa.Column(
            "ordinal",
            sa.Integer(),
            nullable=False,
            comment="Draw order within the sample, from 1.",
        ),
        sa.Column(
            "stratum",
            sa.String(length=100),
            nullable=True,
            comment="Stratum the item was drawn from; null if unstratified.",
        ),
        sa.Column(
            "item",
            postgresql.JSONB(),
            nullable=False,
            comment='What is labelled, e.g. {"posting_id": ...} or {"posting_ids": [a, b]}.',
        ),
        sa.ForeignKeyConstraint(
            ["sample_id"],
            ["ops.sample.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sample_id", "item", name="uq_sample_item_item"),
        sa.UniqueConstraint("sample_id", "ordinal", name="uq_sample_item_ordinal"),
        schema="ops",
        comment="One drawn item of a sample.",
    )
    op.create_table(
        "label",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sample_item_id", sa.Uuid(), nullable=False, comment="The item judged."),
        sa.Column(
            "labeller",
            sa.String(length=100),
            nullable=False,
            comment="Who labelled it, e.g. 'reviewer'.",
        ),
        sa.Column(
            "label",
            postgresql.JSONB(),
            nullable=False,
            comment='The judgement, e.g. {"correct": true}.',
        ),
        sa.Column("note", sa.Text(), nullable=True, comment="Free-text remark by the labeller."),
        sa.Column(
            "labelled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When the label was given.",
        ),
        sa.Column(
            "superseded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the same labeller replaced this label.",
        ),
        sa.ForeignKeyConstraint(
            ["sample_item_id"],
            ["ops.sample_item.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="ops",
        comment="A human judgement on a sample item. Only the project reviewer's labels "
        "count toward a gate.",
    )
    op.create_index(
        "uq_label_current_item_labeller",
        "label",
        ["sample_item_id", "labeller"],
        unique=True,
        schema="ops",
        postgresql_where="superseded_at IS NULL",
    )


def downgrade() -> None:
    """Drop the tables created above."""
    for table in ("label", "sample_item", "sample"):
        op.drop_table(table, schema="ops")
