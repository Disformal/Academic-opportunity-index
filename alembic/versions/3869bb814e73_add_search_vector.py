"""add_search_vector

Revision ID: 3869bb814e73
Revises: b83903f66727
Create Date: 2026-09-25 20:34:56.241022

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3869bb814e73"
down_revision: str | Sequence[str] | None = "b83903f66727"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add tsvector search_vector column, trigger, and GIN index to opportunity."""
    op.execute(
        """
        ALTER TABLE opportunity
        ADD COLUMN IF NOT EXISTS search_vector tsvector;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION opportunity_search_vector_update()
        RETURNS trigger AS $$
        BEGIN
          NEW.search_vector :=
            setweight(to_tsvector('simple', coalesce(NEW.title, '')), 'A') ||
            setweight(to_tsvector('simple', coalesce(NEW.type, '')), 'B') ||
            setweight(to_tsvector('simple', coalesce(NEW.country, '')), 'C');
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_opportunity_search_vector
          BEFORE INSERT OR UPDATE OF title, type, country
          ON opportunity
          FOR EACH ROW EXECUTE FUNCTION opportunity_search_vector_update();
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_opportunity_search_vector
          ON opportunity USING gin(search_vector);
        """
    )

    # Backfill existing rows
    op.execute(
        """
        UPDATE opportunity SET search_vector =
          setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
          setweight(to_tsvector('simple', coalesce(type, '')), 'B') ||
          setweight(to_tsvector('simple', coalesce(country, '')), 'C');
        """
    )


def downgrade() -> None:
    """Remove search_vector column, trigger, and function."""
    op.execute("DROP TRIGGER IF EXISTS trg_opportunity_search_vector ON opportunity;")
    op.execute("DROP FUNCTION IF EXISTS opportunity_search_vector_update();")
    op.execute("DROP INDEX IF EXISTS ix_opportunity_search_vector;")
    op.execute("ALTER TABLE opportunity DROP COLUMN IF EXISTS search_vector;")
