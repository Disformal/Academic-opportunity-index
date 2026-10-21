"""The scheduler (plan §4.3, §4.4) and the discover handler.

One tick reaps expired leases, queues a fetch task for every due target of an enabled source with a
legal basis (ADR 0012: the scheduler refuses sources without one), never a second open task for a
target, and one discover task for an eligible source that has no targets yet. These tests really
commit.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fake_adapter import FakeApiAdapter
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aoi.db.schema.core import Source
from aoi.db.schema.enums import LegalBasis, TargetRole, TaskState
from aoi.db.schema.ops import CrawlTarget, Task
from aoi.tasks.handlers.discover import make_discover_handler
from aoi.tasks.scheduler import Tick, tick
from aoi.tasks.worker import run_once

pytestmark = pytest.mark.integration

AddTarget = Callable[..., uuid.UUID]
AddTasks = Callable[..., list[uuid.UUID]]
LEASE = timedelta(minutes=10)
URL = "https://queue-test-scheduler.example.org/api/jobs?page=1"


def _set_source(engine: Engine, source_id: uuid.UUID, **values: object) -> None:
    with Session(engine) as session, session.begin():
        session.execute(update(Source).where(Source.id == source_id).values(**values))


def _eligible(engine: Engine, source_id: uuid.UUID) -> None:
    _set_source(engine, source_id, enabled=True, legal_basis=LegalBasis.OFFICIAL_API)


def _tick(engine: Engine) -> Tick:
    with Session(engine) as session, session.begin():
        return tick(session, lease=LEASE)


def _tasks(engine: Engine, kind: str) -> list[Task]:
    with Session(engine) as session:
        rows = list(session.scalars(select(Task).where(Task.kind == kind)))
        session.expunge_all()
        return rows


def _target(engine: Engine, target_id: uuid.UUID) -> CrawlTarget:
    with Session(engine) as session:
        target = session.get_one(CrawlTarget, target_id)
        session.expunge(target)
        return target


def test_a_tick_queues_a_fetch_for_each_due_target(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    _eligible(engine, queue_source_id)
    due = add_target(url=URL)
    later = add_target(url=URL.replace("page=1", "page=2"))
    with Session(engine) as session, session.begin():
        session.get_one(CrawlTarget, later).next_due_at = datetime.now(UTC) + timedelta(hours=1)

    report = _tick(engine)

    (task,) = _tasks(engine, "fetch")
    with Session(engine) as session:
        target = session.get_one(CrawlTarget, due)
        assert (task.crawl_target_id, task.host_id, task.source_id) == (
            due,
            target.host_id,
            queue_source_id,
        )
    assert report.fetches == 1


def test_a_source_without_a_legal_basis_is_refused(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    _set_source(engine, queue_source_id, enabled=True, legal_basis=None)
    add_target(url=URL)

    report = _tick(engine)

    assert _tasks(engine, "fetch") == []
    with Session(engine) as session:
        name = session.get_one(Source, queue_source_id).name
    assert name in report.refused


def test_a_disabled_source_is_not_crawled(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    _set_source(engine, queue_source_id, enabled=False, legal_basis=LegalBasis.OFFICIAL_API)
    add_target(url=URL)

    _tick(engine)

    assert _tasks(engine, "fetch") == []


@pytest.mark.parametrize("open_state", [TaskState.QUEUED, TaskState.RUNNING, TaskState.FAILED])
def test_a_target_with_an_open_task_is_not_queued_again(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget, open_state: TaskState
) -> None:
    _eligible(engine, queue_source_id)
    add_target(url=URL)
    _tick(engine)
    with Session(engine) as session, session.begin():
        task = session.scalars(select(Task).where(Task.kind == "fetch")).one()
        task.state = open_state
        if open_state == TaskState.RUNNING:
            task.locked_by, task.locked_at = "w1", datetime.now(UTC)

    _tick(engine)

    assert len(_tasks(engine, "fetch")) == 1


def test_a_target_whose_last_task_is_finished_is_queued_when_due_again(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    _eligible(engine, queue_source_id)
    target_id = add_target(url=URL)
    _tick(engine)
    with Session(engine) as session, session.begin():
        session.scalars(select(Task).where(Task.kind == "fetch")).one().state = TaskState.DEAD
        session.get_one(CrawlTarget, target_id).next_due_at = datetime.now(UTC)

    _tick(engine)

    assert sorted(t.state for t in _tasks(engine, "fetch")) == [TaskState.DEAD, TaskState.QUEUED]


def test_a_target_whose_task_died_waits_for_its_next_regular_time(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    """Whatever killed the task (a rolled-back handler, a lost worker), dead is for review."""
    _eligible(engine, queue_source_id)
    target_id = add_target(url=URL)
    _tick(engine)
    with Session(engine) as session, session.begin():
        session.scalars(select(Task).where(Task.kind == "fetch")).one().state = TaskState.DEAD

    _tick(engine)

    assert [t.state for t in _tasks(engine, "fetch")] == [TaskState.DEAD]
    target = _target(engine, target_id)
    assert target.next_due_at - target.updated_at == target.recrawl_interval


def test_the_database_refuses_a_second_open_fetch_task_for_a_target(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget, add_tasks: AddTasks
) -> None:
    target_id = add_target(url=URL)
    add_tasks(Task(kind="fetch", source_id=queue_source_id, crawl_target_id=target_id))

    with pytest.raises(IntegrityError, match="uq_task_open_target"):
        add_tasks(Task(kind="fetch", source_id=queue_source_id, crawl_target_id=target_id))


def test_a_tick_reaps_expired_leases(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(
        Task(
            kind="fetch",
            source_id=queue_source_id,
            state=TaskState.RUNNING,
            locked_by="killed",
            locked_at=datetime.now(UTC) - LEASE * 2,
            attempts=1,
        )
    )

    report = _tick(engine)

    assert report.reaped == 1
    with Session(engine) as session:
        assert session.get_one(Task, task_id).state == TaskState.FAILED


def test_an_eligible_source_without_targets_gets_one_discover_task(
    engine: Engine, queue_source_id: uuid.UUID
) -> None:
    _eligible(engine, queue_source_id)

    _tick(engine)
    _tick(engine)

    (task,) = _tasks(engine, "discover")
    assert task.source_id == queue_source_id


def test_the_database_refuses_a_second_open_discover_task_for_a_source(
    queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    add_tasks(Task(kind="discover", source_id=queue_source_id))

    with pytest.raises(IntegrityError, match="uq_task_open_discover"):
        add_tasks(Task(kind="discover", source_id=queue_source_id))


def test_a_discover_task_turns_the_adapters_seeds_into_crawl_targets(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget, add_tasks: AddTasks
) -> None:
    _set_source(engine, queue_source_id, base_url="https://queue-test-scheduler.example.org/api")
    add_tasks(Task(kind="discover", source_id=queue_source_id))
    with Session(engine) as session:
        name = session.get_one(Source, queue_source_id).name

    assert run_once(
        engine, {"discover": make_discover_handler({name: FakeApiAdapter()})}, worker="w1"
    )

    with Session(engine) as session:
        (target,) = session.scalars(select(CrawlTarget)).all()
        assert target.url == "https://queue-test-scheduler.example.org/api?page=1"
        assert target.role == TargetRole.LISTING
        assert session.scalars(select(Task.state)).one() == TaskState.DONE


def test_a_retired_target_is_never_queued(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    _eligible(engine, queue_source_id)
    target_id = add_target(url=URL)
    with Session(engine) as session, session.begin():
        session.get_one(CrawlTarget, target_id).retired_at = datetime.now(UTC)

    _tick(engine)

    assert _tasks(engine, "fetch") == []
