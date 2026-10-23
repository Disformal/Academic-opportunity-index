"""The M3 drop (plan §5, ADR 0029, 0032): the old `public` tables, their enum types and functions
are gone at head; only Alembic's own table stays in `public`, with update_updated_at(), which
the new schemas' triggers use. The downgrade recreates the old
schema, empty. The old data lives only in the archived dump (docs/m3-diff-report.md).
"""

from collections.abc import Callable

import pytest
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration

OLD_TABLES = {
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
}
OLD_FUNCTIONS = {"assert_provenance_complete", "opportunity_search_vector_update"}


def _public(engine: Engine) -> tuple[set[str], int, set[str]]:
    """Tables, the number of enum types, and functions in `public`."""
    with engine.connect() as conn:
        tables = set(
            conn.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        )
        enums = conn.scalar(
            text(
                "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
                " WHERE n.nspname = 'public' AND t.typtype = 'e'"
            )
        )
        functions = set(
            conn.scalars(
                text(
                    "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                    " WHERE n.nspname = 'public'"
                )
            )
        )
    return tables, enums or 0, functions


def test_the_old_tables_are_gone_and_the_downgrade_recreates_them_empty(
    engine: Engine, migrate: Callable[..., None]
) -> None:
    assert _public(engine) == ({"alembic_version"}, 0, {"update_updated_at"})

    try:
        migrate("downgrade", "-1")
        tables, enums, functions = _public(engine)
        assert tables == OLD_TABLES | {"alembic_version"}
        assert (enums, functions) == (7, OLD_FUNCTIONS | {"update_updated_at"})
        with engine.connect() as conn:
            rows = sum(
                conn.scalar(text(f"SELECT count(*) FROM public.{table}")) or 0
                for table in OLD_TABLES
            )
        assert rows == 0
    finally:
        migrate("upgrade", "head")

    assert _public(engine) == ({"alembic_version"}, 0, {"update_updated_at"})
