"""m1_layer_schemas

Create the four layer schemas of ADR 0007: evidence (L0), core (L1 + L2),
analytics (L3) and ops (queue, runs, labels).

Revision ID: a145a3f6a110
Revises: e18d4fac0019
Create Date: 2026-10-17 13:28:23.919548

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a145a3f6a110"
down_revision: str | Sequence[str] | None = "e18d4fac0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMAS = ["evidence", "core", "analytics", "ops"]


def upgrade() -> None:
    """Create the layer schemas."""
    for schema in _SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def downgrade() -> None:
    """Drop the layer schemas (RESTRICT: fails if a later revision left tables behind)."""
    for schema in reversed(_SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} RESTRICT")
