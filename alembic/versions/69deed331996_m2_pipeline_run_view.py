"""m2_pipeline_run_view

Run accounting (plan §4.3, ADR 0030 §6): ops.pipeline_run, a view over ops.task per source, UTC
hour of creation and kind. The Grafana read-only role (docker/init-grafana-ro.sql) may read
exactly what the Pipeline (tasks) dashboard needs: ops.task, this view and core.source. The grant
is skipped on a server without that role.

Revision ID: 69deed331996
Revises: 4f9333c8f24b
Create Date: 2026-10-18 16:19:55.522705

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "69deed331996"
down_revision: str | Sequence[str] | None = "4f9333c8f24b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GRAFANA_TABLES = "ops.task, ops.pipeline_run, core.source"


def _if_grafana_role(statement: str) -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aoi_grafana_ro') THEN
                {statement};
            END IF;
        END
        $$;
    """)


def upgrade() -> None:
    """Create the view and grant the Grafana role read access to the dashboard's relations."""
    op.execute("""
        CREATE VIEW ops.pipeline_run AS
        SELECT
            t.source_id,
            s.name AS source,
            date_trunc('hour', t.created_at, 'UTC') AS hour,
            t.kind,
            count(*) AS tasks,
            count(*) FILTER (WHERE t.state = 'queued') AS queued,
            count(*) FILTER (WHERE t.state = 'running') AS running,
            count(*) FILTER (WHERE t.state = 'failed') AS failed,
            count(*) FILTER (WHERE t.state = 'done') AS done,
            count(*) FILTER (WHERE t.state = 'dead') AS dead,
            sum(t.attempts) AS attempts,
            max(t.updated_at) FILTER (WHERE t.state IN ('done', 'dead')) AS last_finished_at
        FROM ops.task t
        JOIN core.source s ON s.id = t.source_id
        GROUP BY t.source_id, s.name, date_trunc('hour', t.created_at, 'UTC'), t.kind
    """)
    op.execute(
        "COMMENT ON VIEW ops.pipeline_run IS "
        "'Tasks per source, UTC hour of creation and kind, by state (ADR 0030).'"
    )
    _if_grafana_role("GRANT USAGE ON SCHEMA ops, core TO aoi_grafana_ro")
    _if_grafana_role(f"GRANT SELECT ON {_GRAFANA_TABLES} TO aoi_grafana_ro")


def downgrade() -> None:
    """Revoke the grants and drop the view."""
    _if_grafana_role(f"REVOKE SELECT ON {_GRAFANA_TABLES} FROM aoi_grafana_ro")
    _if_grafana_role("REVOKE USAGE ON SCHEMA ops, core FROM aoi_grafana_ro")
    op.execute("DROP VIEW IF EXISTS ops.pipeline_run")
