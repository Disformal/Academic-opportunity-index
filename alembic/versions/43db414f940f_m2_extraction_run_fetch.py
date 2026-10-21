"""m2_extraction_run_fetch

One extraction per fetch per extractor version (ADR 0030 §2): core.extraction_run.fetch_id with
UNIQUE (fetch_id, extractor, extractor_version). The table is empty when this runs (nothing wrote
to it before M2), so the column is added NOT NULL directly.

Revision ID: 43db414f940f
Revises: 388aaad8914e
Create Date: 2026-10-18 15:41:38.043101

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "43db414f940f"
down_revision: str | Sequence[str] | None = "388aaad8914e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add fetch_id, its foreign key, and the one-run-per-version constraint."""
    op.add_column(
        "extraction_run",
        sa.Column(
            "fetch_id",
            sa.Uuid(),
            nullable=False,
            comment="The document extracted; one run per fetch per extractor version (ADR 0030).",
        ),
        schema="core",
    )
    op.create_foreign_key(
        "extraction_run_fetch_id_fkey",
        "extraction_run",
        "fetch",
        ["fetch_id"],
        ["id"],
        source_schema="core",
        referent_schema="evidence",
    )
    op.create_unique_constraint(
        "uq_extraction_run_fetch_version",
        "extraction_run",
        ["fetch_id", "extractor", "extractor_version"],
        schema="core",
    )


def downgrade() -> None:
    """Reverse the upgrade."""
    op.drop_constraint(
        "uq_extraction_run_fetch_version", "extraction_run", schema="core", type_="unique"
    )
    op.drop_constraint(
        "extraction_run_fetch_id_fkey", "extraction_run", schema="core", type_="foreignkey"
    )
    op.drop_column("extraction_run", "fetch_id", schema="core")
