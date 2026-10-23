"""m3_drop_old_public_tables

The M3 drop (plan §5; ADR 0029, 0032): the pre-M3 `public` tables, their enum types and
old-only functions. Their data was archived first (`pg_dump -n public`, restored and checked;
see docs/m3-diff-report.md) and is a reference only. Alembic's own table stays in `public`, and
so does update_updated_at(), which the new schemas' updated_at triggers use.

The downgrade recreates the old schema, empty, from f1a4f7543e91_old_public_schema.sql (a
schema-only dump of what the earlier migrations built), so earlier downgrades find it as they
left it.

Revision ID: f1a4f7543e91
Revises: 07b00ccfa252
Create Date: 2026-10-22 01:20:35.012345

"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a4f7543e91"
down_revision: str | Sequence[str] | None = "07b00ccfa252"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "crawl_url",
    "duplicate_cluster",
    "duplicate_member",
    "extraction_run",
    "fetch",
    "field_assertion",
    "opportunity",
    "opportunity_version",
    "organization",
    "pipeline_run",
    "robots_assessment",
    "source",
    "source_document",
)
_FUNCTIONS = ("assert_provenance_complete", "opportunity_search_vector_update")
_TYPES = (
    "closure_reason",
    "derivation",
    "fetch_outcome",
    "identity_kind",
    "opportunity_status",
    "page_role",
    "publication_state",
)
_SCHEMA_SQL = Path(__file__).with_name("f1a4f7543e91_old_public_schema.sql")


def upgrade() -> None:
    """Drop the old tables together (their foreign keys need no CASCADE), functions, types."""
    op.execute("DROP TABLE " + ", ".join(f"public.{t}" for t in _TABLES))
    op.execute("DROP FUNCTION " + ", ".join(f"public.{f}()" for f in _FUNCTIONS))
    op.execute("DROP TYPE " + ", ".join(f"public.{t}" for t in _TYPES))


def downgrade() -> None:
    """Recreate the old schema, empty. Through the DBAPI cursor without parameters: the dump
    holds several statements and a literal '%' that a parameterised call would misread."""
    op.get_bind().connection.dbapi_connection.cursor().execute(_SCHEMA_SQL.read_text())
