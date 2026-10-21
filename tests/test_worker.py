"""The worker loop (ADR 0014, 0030): a handler's writes commit with `done`, or not at all.

These tests really commit: run_once claims in its own transaction and commits the result.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from aoi.db.schema.enums import TaskState
from aoi.db.schema.ops import Task
from aoi.tasks.queue import reap
from aoi.tasks.worker import AttemptFailed, run, run_once

pytestmark = pytest.mark.integration

AddTasks = Callable[..., list[uuid.UUID]]


def _enqueue_child(session: Session, task: Task) -> None:
    session.add(Task(kind="child", source_id=task.source_id, payload={"parent": str(task.id)}))


def _children(engine: Engine) -> list[str]:
    with Session(engine) as session:
        return [
            p["parent"] for p in session.scalars(select(Task.payload).where(Task.kind == "child"))
        ]


def test_run_once_commits_the_handler_writes_with_done(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id))

    assert run_once(engine, {"fetch": _enqueue_child}, worker="w1") is True

    with Session(engine) as session:
        assert session.get_one(Task, task_id).state == TaskState.DONE
    assert _children(engine) == [str(task_id)]


def test_run_once_records_a_handler_error_and_discards_its_writes(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id))

    def broken(session: Session, task: Task) -> None:
        _enqueue_child(session, task)
        raise ValueError("no job list on page")

    run_once(engine, {"fetch": broken}, worker="w1")

    with Session(engine) as session:
        task = session.get_one(Task, task_id)
        assert task.state == TaskState.FAILED
        assert task.last_error == "ValueError: no job list on page"
    assert _children(engine) == []


def test_run_once_discards_the_result_when_the_lease_was_lost(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id))

    def too_slow(session: Session, task: Task) -> None:
        _enqueue_child(session, task)
        with Session(engine) as reaper:
            reap(reaper, lease=timedelta(0))
            reaper.commit()

    run_once(engine, {"fetch": too_slow}, worker="w1")

    with Session(engine) as session:
        task = session.get_one(Task, task_id)
        assert task.state == TaskState.FAILED
        assert task.last_error is not None
        assert task.last_error.startswith("LeaseExpired")
    assert _children(engine) == []


def test_run_once_claims_only_kinds_it_has_handlers_for(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(Task(kind="extract", source_id=queue_source_id))

    assert run_once(engine, {"fetch": _enqueue_child}, worker="w1") is False

    with Session(engine) as session:
        assert session.get_one(Task, task_id).state == TaskState.QUEUED


def test_run_drains_the_queue_until_told_to_stop(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    ids = add_tasks(*(Task(kind="fetch", source_id=queue_source_id) for _ in range(5)))
    idle_polls = 0

    def stop_when_idle() -> bool:
        return idle_polls > 0

    def on_idle() -> None:
        nonlocal idle_polls
        idle_polls += 1

    run(engine, {"fetch": _enqueue_child}, worker="w1", should_stop=stop_when_idle, idle=on_idle)

    assert sorted(_children(engine)) == sorted(str(i) for i in ids)


def test_run_once_keeps_the_reaper_record_when_a_handler_fails_after_losing_its_lease(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id))

    def too_slow_then_broken(session: Session, task: Task) -> None:
        with Session(engine) as reaper:
            reap(reaper, lease=timedelta(0))
            reaper.commit()
        raise ValueError("no job list on page")

    run_once(engine, {"fetch": too_slow_then_broken}, worker="w1")

    with Session(engine) as session:
        task = session.get_one(Task, task_id)
        assert task.state == TaskState.FAILED
        assert task.attempts == 1
        assert task.last_error is not None
        assert task.last_error.startswith("LeaseExpired")


def test_an_attempt_failed_error_keeps_the_handler_writes_and_records_the_failure(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id))

    def records_then_fails(session: Session, task: Task) -> None:
        _enqueue_child(session, task)  # stands in for the fetch row of a failed request
        raise AttemptFailed("HTTP 503 from inspirehep.net")

    run_once(engine, {"fetch": records_then_fails}, worker="w1")

    with Session(engine) as session:
        task = session.get_one(Task, task_id)
        assert task.state == TaskState.FAILED
        assert task.last_error == "AttemptFailed: HTTP 503 from inspirehep.net"
    assert _children(engine) == [str(task_id)]
