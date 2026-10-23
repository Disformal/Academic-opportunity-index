"""m3_placeholder_stated_dates

Placeholder years have no stated time (ADR 0036): versions resolved before the rule had their
stated_deadline_at / stated_posted_at set from dates like 8888-12-31. Those columns are derived
from the version's snapshot, which is unchanged; the upgrade clears them, the downgrade derives
them again from the snapshot as resolve did before (a bare date is midnight UTC).

Revision ID: 07b00ccfa252
Revises: ff6e6f4fa217
Create Date: 2026-10-21 23:49:07.580541

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "07b00ccfa252"
down_revision: str | Sequence[str] | None = "ff6e6f4fa217"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = {"stated_deadline_at": "deadline", "stated_posted_at": "posted_at"}


def upgrade() -> None:
    """Clear stated times whose year is outside 1900-2200."""
    for column in _COLUMNS:
        op.execute(
            f"UPDATE core.posting_history SET {column} = NULL "
            f"WHERE extract(year FROM {column} AT TIME ZONE 'UTC') NOT BETWEEN 1900 AND 2200"
        )


def downgrade() -> None:
    """Derive the cleared stated times from the snapshot again."""
    for column, field in _COLUMNS.items():
        value = f"(snapshot->'fields'->>'{field}')"
        op.execute(
            f"UPDATE core.posting_history SET {column} = CASE WHEN length({value}) = 10 "
            f"THEN {value}::date::timestamp AT TIME ZONE 'UTC' ELSE {value}::timestamptz END "
            f"WHERE {column} IS NULL AND {value} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' "
            f"AND substr({value}, 1, 4)::int NOT BETWEEN 1900 AND 2200"
        )
