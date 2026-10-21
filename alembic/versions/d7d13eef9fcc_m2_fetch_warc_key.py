"""m2_fetch_warc_key

WARC evidence (ADR 0006, 0030 §3): evidence.fetch.warc_key, the S3 key of the fetch's WARC object,
set together with warc_record_id (both or neither).

Revision ID: d7d13eef9fcc
Revises: 07a311ccbe27
Create Date: 2026-10-18 12:39:33.766302

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7d13eef9fcc"
down_revision: str | Sequence[str] | None = "07a311ccbe27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_RECORD_ID_COMMENT = "WARC-Record-ID of the stored response; null until the M2 writer."
_NEW_RECORD_ID_COMMENT = (
    "WARC-Record-ID of the response or revisit record; null if no response came."
)


def upgrade() -> None:
    """Add warc_key, pair it with warc_record_id, and update the record id's comment."""
    op.add_column(
        "fetch",
        sa.Column(
            "warc_key",
            sa.Text(),
            nullable=True,
            comment="S3 key of the WARC object, stored before this row (ADR 0027, 0030).",
        ),
        schema="evidence",
    )
    op.create_check_constraint(
        "ck_fetch_warc_pair",
        "fetch",
        "(warc_key IS NULL) = (warc_record_id IS NULL)",
        schema="evidence",
    )
    op.alter_column(
        "fetch",
        "warc_record_id",
        existing_type=sa.String(length=100),
        comment=_NEW_RECORD_ID_COMMENT,
        existing_comment=_OLD_RECORD_ID_COMMENT,
        schema="evidence",
    )


def downgrade() -> None:
    """Reverse the upgrade."""
    op.alter_column(
        "fetch",
        "warc_record_id",
        existing_type=sa.String(length=100),
        comment=_OLD_RECORD_ID_COMMENT,
        existing_comment=_NEW_RECORD_ID_COMMENT,
        schema="evidence",
    )
    op.drop_constraint("ck_fetch_warc_pair", "fetch", schema="evidence", type_="check")
    op.drop_column("fetch", "warc_key", schema="evidence")
