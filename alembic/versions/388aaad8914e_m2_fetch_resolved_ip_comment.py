"""m2_fetch_resolved_ip_comment

evidence.fetch.resolved_ip holds the address validate_host resolved for the final URL's host. In
production requests go through the egress proxy (ADR 0023), so it is not necessarily the address
this process connected to; the comment now says what the value is.

Revision ID: 388aaad8914e
Revises: d7d13eef9fcc
Create Date: 2026-10-18 12:56:45.158455

"""

from collections.abc import Sequence

from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "388aaad8914e"
down_revision: str | Sequence[str] | None = "d7d13eef9fcc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "Address the final hop connected to."
_NEW = "Address validate_host resolved for the final URL's host (ADR 0023)."


def upgrade() -> None:
    """Correct the resolved_ip column comment."""
    op.alter_column(
        "fetch",
        "resolved_ip",
        existing_type=postgresql.INET(),
        comment=_NEW,
        existing_comment=_OLD,
        schema="evidence",
    )


def downgrade() -> None:
    """Restore the previous comment."""
    op.alter_column(
        "fetch",
        "resolved_ip",
        existing_type=postgresql.INET(),
        comment=_OLD,
        existing_comment=_NEW,
        schema="evidence",
    )
