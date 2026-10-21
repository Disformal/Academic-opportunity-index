"""Run accounting (plan §4.3, ADR 0030 §6): the ops.pipeline_run view and the tasks dashboard.

ops.pipeline_run aggregates ops.task per source, UTC hour of creation and kind. Every SQL query in
the Grafana "Pipeline (tasks)" dashboard is run as the Grafana role (docker/init-grafana-ro.sql),
interpolated as Grafana really interpolates it (an earlier model that guessed instead hid a
quoting bug), so a broken panel or a missing grant fails here, not as an empty panel. These
tests really commit.
"""

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from aoi.db.schema.core import Source
from aoi.db.schema.enums import TaskState
from aoi.db.schema.ops import Task

pytestmark = pytest.mark.integration

AddTasks = Callable[..., list[uuid.UUID]]
DASHBOARD = Path(__file__).parent.parent / "grafana" / "dashboards" / "pipeline-tasks.json"
NINE = datetime(2026, 10, 18, 9, 15, tzinfo=UTC)
TEN = datetime(2026, 10, 18, 10, 40, tzinfo=UTC)


def _task(source_id: uuid.UUID, kind: str, state: TaskState, at: datetime, **kw: object) -> Task:
    if state == TaskState.RUNNING:
        kw = {"locked_by": "w1", "locked_at": at, **kw}
    return Task(kind=kind, source_id=source_id, state=state, created_at=at, **kw)


def test_pipeline_run_counts_tasks_per_source_hour_and_kind(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    add_tasks(
        _task(queue_source_id, "fetch", TaskState.DONE, NINE, attempts=1),
        _task(queue_source_id, "fetch", TaskState.DONE, NINE, attempts=2),
        _task(queue_source_id, "fetch", TaskState.DEAD, NINE, attempts=5, last_error="503"),
        _task(queue_source_id, "fetch", TaskState.QUEUED, TEN),
        _task(queue_source_id, "fetch", TaskState.FAILED, TEN, attempts=1),
        _task(queue_source_id, "fetch", TaskState.RUNNING, TEN, attempts=1),
        _task(queue_source_id, "extract", TaskState.DONE, NINE, attempts=1),
    )

    with Session(engine) as session:
        name = session.get_one(Source, queue_source_id).name
        rows = session.execute(
            text(
                "SELECT source, hour, kind, tasks, queued, running, failed, done, dead, attempts "
                "FROM ops.pipeline_run WHERE source_id = :id ORDER BY hour, kind"
            ),
            {"id": queue_source_id},
        ).all()

    nine, ten = (datetime(2026, 10, 18, h, tzinfo=UTC) for h in (9, 10))
    assert [tuple(r) for r in rows] == [
        (name, nine, "extract", 1, 0, 0, 0, 1, 0, 1),
        (name, nine, "fetch", 3, 0, 0, 0, 2, 1, 8),
        (name, ten, "fetch", 3, 1, 1, 1, 0, 0, 2),
    ]


def test_pipeline_run_reports_when_the_last_task_finished(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    done, _ = add_tasks(
        _task(queue_source_id, "fetch", TaskState.DONE, NINE),
        _task(queue_source_id, "fetch", TaskState.QUEUED, NINE),
    )
    with Session(engine) as session:
        finished = session.get_one(Task, done).updated_at
        last = session.execute(
            text("SELECT last_finished_at FROM ops.pipeline_run WHERE source_id = :id"),
            {"id": queue_source_id},
        ).scalar_one()

    assert last == finished


def _dashboard_queries() -> list[tuple[str, str]]:
    dashboard = json.loads(DASHBOARD.read_text())
    queries = [(v["name"], v["query"]) for v in dashboard["templating"]["list"]]
    queries += [
        (panel["title"], target["rawSql"])
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]
    return queries


def _interpolate_like_grafana(sql: str, selected: list[str]) -> str:
    """What Grafana sends to Postgres for this dashboard, for the selected source ids.

    For a SQL datasource Grafana renders a variable as its values single-quoted and comma-joined;
    "All" (no custom all value) selects every value. Anything left with a `$` afterwards is a
    variable or macro this model does not know, and fails the test rather than being guessed at.
    """
    sql = re.sub(
        r"\$__timeFilter\(([^)]+)\)", r"\1 BETWEEN now() - interval '7 days' AND now()", sql
    )
    quoted = ",".join("'" + value.replace("'", "''") + "'" for value in selected)
    sql = sql.replace("${source:sqlstring}", quoted).replace("$source", quoted)
    assert "$" not in sql, f"unexpanded Grafana variable or macro in: {sql}"
    return sql


def test_the_dashboard_has_panels() -> None:
    assert len(_dashboard_queries()) >= 5


@pytest.mark.parametrize("selection", ["all sources", "one source"])
@pytest.mark.parametrize("name, sql", _dashboard_queries())
def test_every_dashboard_query_runs_as_the_grafana_role(
    engine: Engine,
    queue_source_id: uuid.UUID,
    add_tasks: AddTasks,
    name: str,
    sql: str,
    selection: str,
) -> None:
    add_tasks(
        _task(queue_source_id, "fetch", TaskState.DEAD, NINE, attempts=5, last_error="HTTP 503"),
        _task(queue_source_id, "fetch", TaskState.FAILED, TEN, attempts=1, last_error="timeout"),
    )
    with engine.connect() as conn:
        role = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'aoi_grafana_ro'"))
        assert role.first(), "the Grafana role is missing; see docker/init-grafana-ro.sql"
        selected = (
            [str(i) for i in conn.execute(text("SELECT id FROM core.source")).scalars()]
            if selection == "all sources"
            else [str(queue_source_id)]
        )
        conn.execute(text("SET LOCAL ROLE aoi_grafana_ro"))
        conn.execute(text(_interpolate_like_grafana(sql, selected))).all()


def _panel_query(title: str) -> str:
    return next(sql for name, sql in _dashboard_queries() if name == title)


def test_the_dead_tasks_panel_counts_only_tasks_awaiting_review(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    reviewed, _unreviewed = add_tasks(
        _task(queue_source_id, "fetch", TaskState.DEAD, NINE, attempts=5, last_error="HTTP 503"),
        _task(queue_source_id, "fetch", TaskState.DEAD, TEN, attempts=5, last_error="HTTP 503"),
    )
    add_tasks(_task(queue_source_id, "fetch", TaskState.QUEUED, TEN, retry_of=reviewed))

    with engine.connect() as conn:
        conn.execute(text("SET LOCAL ROLE aoi_grafana_ro"))
        sql = _interpolate_like_grafana(_panel_query("Dead tasks"), [str(queue_source_id)])
        assert conn.execute(text(sql)).scalar_one() == 1
