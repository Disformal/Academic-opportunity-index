"""Requeueing a dead task after review (plan §4.3: `dead` waits for review).

A requeue inserts a fresh task linked to the dead one through `retry_of`; the dead row is never
changed, so its failure stays in the record and in past run accounting. These tests really commit.
"""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aoi.db.schema.core import Source
from aoi.db.schema.enums import LegalBasis, TaskState
from aoi.db.schema.ops import CrawlTarget, Task
from aoi.tasks.queue import NotRequeueable, claim, requeue

pytestmark = pytest.mark.integration

AddTarget = Callable[..., uuid.UUID]
AddTasks = Callable[..., list[uuid.UUID]]
URL = "https://queue-test-requeue.example.org/api/jobs?page=1"


def _eligible(engine: Engine, source_id: uuid.UUID, **values: object) -> None:
    values = {"enabled": True, "legal_basis": LegalBasis.OFFICIAL_API, **values}
    with Session(engine) as session, session.begin():
        session.execute(update(Source).where(Source.id == source_id).values(**values))


def _dead(source_id: uuid.UUID, **kw: object) -> Task:
    return Task(
        source_id=source_id,
        state=TaskState.DEAD,
        attempts=5,
        last_error="HTTPStatusError: 503",
        **{"kind": "fetch", **kw},
    )


def _requeue(engine: Engine, task_id: uuid.UUID) -> uuid.UUID:
    with Session(engine) as session, session.begin():
        return requeue(session, task_id).id


def test_requeue_adds_a_fresh_task_linked_to_the_dead_one(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget, add_tasks: AddTasks
) -> None:
    _eligible(engine, queue_source_id)
    target_id = add_target(url=URL)
    with Session(engine) as session:
        host_id = session.get_one(CrawlTarget, target_id).host_id
    (dead_id,) = add_tasks(
        _dead(
            queue_source_id,
            crawl_target_id=target_id,
            host_id=host_id,
            priority=3,
            payload={"page": 1},
        )
    )

    new_id = _requeue(engine, dead_id)

    with Session(engine) as session:
        dead = session.get_one(Task, dead_id)
        new = session.get_one(Task, new_id)
        assert (dead.state, dead.attempts, dead.last_error) == (
            TaskState.DEAD,
            5,
            "HTTPStatusError: 503",
        )
        assert (new.state, new.attempts, new.last_error, new.retry_of) == (
            TaskState.QUEUED,
            0,
            None,
            dead_id,
        )
        assert (new.kind, new.source_id, new.crawl_target_id, new.host_id) == (
            dead.kind,
            dead.source_id,
            dead.crawl_target_id,
            dead.host_id,
        )
        assert (new.priority, new.payload) == (3, {"page": 1})


def test_a_requeued_task_is_claimable_at_once(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    _eligible(engine, queue_source_id)
    (dead_id,) = add_tasks(_dead(queue_source_id, kind="discover"))
    new_id = _requeue(engine, dead_id)

    with Session(engine) as session:
        claimed = claim(session, kinds=["discover"], worker="w1")

    assert claimed is not None
    assert claimed.id == new_id


@pytest.mark.parametrize("state", [TaskState.QUEUED, TaskState.FAILED, TaskState.DONE])
def test_requeue_refuses_a_task_that_is_not_dead(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, state: TaskState
) -> None:
    _eligible(engine, queue_source_id)
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id, state=state))

    with pytest.raises(NotRequeueable):
        _requeue(engine, task_id)


def test_requeue_refuses_an_unknown_task(engine: Engine, queue_source_id: uuid.UUID) -> None:
    with pytest.raises(NotRequeueable):
        _requeue(engine, uuid.uuid4())


def test_requeue_refuses_a_dead_task_requeued_before(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    _eligible(engine, queue_source_id)
    (dead_id,) = add_tasks(_dead(queue_source_id, kind="discover"))
    _requeue(engine, dead_id)

    with pytest.raises(NotRequeueable):
        _requeue(engine, dead_id)


def test_the_database_allows_one_retry_per_dead_task(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (dead_id,) = add_tasks(_dead(queue_source_id))
    add_tasks(Task(kind="fetch", source_id=queue_source_id, retry_of=dead_id))

    with pytest.raises(IntegrityError):
        add_tasks(Task(kind="fetch", source_id=queue_source_id, retry_of=dead_id))


def test_requeue_refuses_a_target_that_already_has_an_open_task(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget, add_tasks: AddTasks
) -> None:
    _eligible(engine, queue_source_id)
    target_id = add_target(url=URL)
    (dead_id, _open) = add_tasks(
        _dead(queue_source_id, crawl_target_id=target_id),
        Task(kind="fetch", source_id=queue_source_id, crawl_target_id=target_id),
    )

    with pytest.raises(NotRequeueable):
        _requeue(engine, dead_id)


def test_requeue_refuses_a_source_that_already_has_an_open_discover(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    _eligible(engine, queue_source_id)
    (dead_id, _open) = add_tasks(
        _dead(queue_source_id, kind="discover"),
        Task(kind="discover", source_id=queue_source_id),
    )

    with pytest.raises(NotRequeueable):
        _requeue(engine, dead_id)


@pytest.mark.parametrize(
    "values", [{"enabled": False}, {"legal_basis": None}], ids=["disabled", "no legal basis"]
)
def test_requeue_refuses_a_source_the_scheduler_would_refuse(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, values: dict[str, object]
) -> None:
    _eligible(engine, queue_source_id, **values)
    (dead_id,) = add_tasks(_dead(queue_source_id, kind="discover"))

    with pytest.raises(NotRequeueable):
        _requeue(engine, dead_id)

    with Session(engine) as session:
        assert session.scalars(select(Task).where(Task.retry_of == dead_id)).first() is None


def test_the_requeue_command_queues_the_retry(
    engine: Engine,
    queue_source_id: uuid.UUID,
    add_tasks: AddTasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aoi import cli
    from aoi.db import session as db_session_module

    _eligible(engine, queue_source_id)
    (dead_id,) = add_tasks(_dead(queue_source_id, kind="discover"))
    monkeypatch.setattr(db_session_module, "get_engine", lambda: engine)

    cli.main(["task", "requeue", str(dead_id)])

    with Session(engine) as session:
        (new,) = session.scalars(select(Task).where(Task.retry_of == dead_id)).all()
        assert new.state == TaskState.QUEUED


def test_the_requeue_command_exits_with_an_error_when_refused(
    engine: Engine,
    queue_source_id: uuid.UUID,
    add_tasks: AddTasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aoi import cli
    from aoi.db import session as db_session_module

    (done_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id, state=TaskState.DONE))
    monkeypatch.setattr(db_session_module, "get_engine", lambda: engine)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["task", "requeue", str(done_id)])

    assert exit_info.value.code != 0
