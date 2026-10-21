"""The long-running commands (plan §4.3): aoi worker and aoi scheduler.

A worker stops on SIGTERM only after finishing its current task; the scheduler commits each tick.
These tests run real processes or the real CLI against the test database.
"""

import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from aoi.adapters.registry import ADAPTERS
from aoi.db.schema.core import Source
from aoi.db.schema.enums import LegalBasis, TaskState
from aoi.db.schema.ops import Task
from aoi.tasks.handlers import handlers_for

pytestmark = pytest.mark.integration

AddTasks = Callable[..., list[uuid.UUID]]
AddTarget = Callable[..., uuid.UUID]
CHAOS_WORKER = Path(__file__).parent / "chaos_worker.py"


def _wait_for(condition: Callable[[], bool], seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_handlers_for_returns_a_handler_for_each_requested_kind() -> None:
    handlers = handlers_for(["fetch", "extract", "discover"], ADAPTERS)

    assert sorted(handlers) == ["discover", "extract", "fetch"]


def test_handlers_for_refuses_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="fecth"):
        handlers_for(["fecth"], ADAPTERS)


def test_a_worker_stops_on_sigterm_only_after_finishing_its_current_task(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    ids = add_tasks(*(Task(kind="chaos", source_id=queue_source_id) for _ in range(5)))
    worker = subprocess.Popen([sys.executable, str(CHAOS_WORKER)], env=os.environ.copy())

    def running() -> list[uuid.UUID]:
        with Session(engine) as session:
            return list(session.scalars(select(Task.id).where(Task.state == TaskState.RUNNING)))

    try:
        _wait_for(lambda: len(running()) == 1)
        (in_flight,) = running()
        worker.send_signal(signal.SIGTERM)
        assert worker.wait(timeout=20) == 0
    finally:
        worker.kill()
        worker.wait()

    with Session(engine) as session:
        states = {t.id: t.state for t in session.scalars(select(Task).where(Task.id.in_(ids)))}
    assert states[in_flight] == TaskState.DONE
    assert TaskState.QUEUED in states.values(), "the worker kept claiming after SIGTERM"
    assert TaskState.RUNNING not in states.values()


def test_the_worker_command_starts_and_stops_cleanly(engine: Engine) -> None:
    env = {**os.environ, "AOI_DATABASE_URL": os.environ["AOI_TEST_DATABASE_URL"]}
    worker = subprocess.Popen(
        [sys.executable, "-m", "aoi.cli", "worker", "--kinds", "fetch", "--idle", "0.05"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert worker.stdout is not None
        _wait_for(lambda: "worker.started" in (worker.stdout.readline() or ""))
        worker.send_signal(signal.SIGTERM)
        assert worker.wait(timeout=20) == 0
    finally:
        worker.kill()
        worker.wait()


def test_the_scheduler_command_commits_one_tick_with_once(
    engine: Engine,
    queue_source_id: uuid.UUID,
    add_target: AddTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aoi import cli
    from aoi.db import session as db_session_module

    with Session(engine) as session, session.begin():
        session.execute(
            update(Source)
            .where(Source.id == queue_source_id)
            .values(enabled=True, legal_basis=LegalBasis.OFFICIAL_API)
        )
    target_id = add_target(url="https://queue-test-commands.example.org/jobs")
    monkeypatch.setattr(db_session_module, "get_engine", lambda: engine)

    cli.main(["scheduler", "--once"])

    with Session(engine) as session:
        (task,) = session.scalars(select(Task).where(Task.kind == "fetch")).all()
        assert (task.kind, task.crawl_target_id) == ("fetch", target_id)
        assert task.created_at <= datetime.now(UTC)
