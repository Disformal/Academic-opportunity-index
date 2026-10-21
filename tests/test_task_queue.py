"""ops.task (ADR 0014, 0030): claiming with SKIP LOCKED, completing and failing under a lease,
and reaping expired leases.

Every test here really commits, because a claim is its own transaction and completion must
commit together with the handler's results. Cleanup truncates ops.task and removes the test
source.
"""

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from aoi.db.schema.enums import TaskState
from aoi.db.schema.ops import Task
from aoi.tasks.queue import MAX_ATTEMPTS, LeaseLost, backoff, claim, complete, fail, reap

pytestmark = pytest.mark.integration

AddTasks = Callable[..., list[uuid.UUID]]


def test_claim_skips_a_task_locked_by_another_transaction(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    first, second = add_tasks(
        Task(kind="fetch", source_id=queue_source_id, priority=10),
        Task(kind="fetch", source_id=queue_source_id, priority=0),
    )

    with Session(engine) as holder:
        holder.execute(select(Task).where(Task.id == first).with_for_update())
        with Session(engine) as session:
            # Without SKIP LOCKED the claim would wait on `holder` forever; fail instead.
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            claimed = claim(session, kinds=["fetch"], worker="w2")
        holder.rollback()

    assert claimed is not None
    assert claimed.id == second


def test_claim_commits_the_lease_and_counts_the_attempt(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id))

    with Session(engine) as session:
        claim(session, kinds=["fetch"], worker="laptop:1234")

    with Session(engine) as other:
        task = other.get_one(Task, task_id)
        assert task.state == TaskState.RUNNING
        assert task.locked_by == "laptop:1234"
        assert task.locked_at is not None
        assert task.attempts == 1


def test_concurrent_claimers_never_get_the_same_task(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    ids = add_tasks(*(Task(kind="fetch", source_id=queue_source_id) for _ in range(40)))

    def drain(worker: str) -> list[uuid.UUID]:
        claimed: list[uuid.UUID] = []
        with Session(engine) as session:
            while (task := claim(session, kinds=["fetch"], worker=worker)) is not None:
                claimed.append(task.id)
        return claimed

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(drain, [f"w{i}" for i in range(8)]))

    all_claimed = [task_id for result in results for task_id in result]
    assert sorted(all_claimed) == sorted(ids)


def test_claim_ignores_other_kinds_and_tasks_not_yet_due(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    later = datetime.now(UTC) + timedelta(hours=1)
    add_tasks(
        Task(kind="extract", source_id=queue_source_id),
        Task(kind="fetch", source_id=queue_source_id, run_after=later),
    )

    with Session(engine) as session:
        assert claim(session, kinds=["fetch"], worker="w1") is None


def test_claim_takes_a_retry_whose_backoff_has_passed(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    earlier = datetime.now(UTC) - timedelta(minutes=1)
    (task_id,) = add_tasks(
        Task(
            kind="fetch",
            source_id=queue_source_id,
            state=TaskState.FAILED,
            attempts=1,
            last_error="TimeoutException: read timed out",
            run_after=earlier,
        ),
    )

    with Session(engine) as session:
        claimed = claim(session, kinds=["fetch"], worker="w1")

    assert claimed is not None
    assert claimed.id == task_id
    assert claimed.attempts == 2


def test_claim_order_is_priority_then_due_time(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    now = datetime.now(UTC)
    low_old, high_new, high_old = add_tasks(
        Task(
            kind="fetch", source_id=queue_source_id, priority=0, run_after=now - timedelta(hours=2)
        ),
        Task(
            kind="fetch",
            source_id=queue_source_id,
            priority=5,
            run_after=now - timedelta(minutes=1),
        ),
        Task(
            kind="fetch", source_id=queue_source_id, priority=5, run_after=now - timedelta(hours=1)
        ),
    )

    order = []
    with Session(engine) as session:
        while (task := claim(session, kinds=["fetch"], worker="w1")) is not None:
            order.append(task.id)

    assert order == [high_old, high_new, low_old]


def _running(source_id: uuid.UUID, *, worker: str, since: timedelta, attempts: int = 1) -> Task:
    return Task(
        kind="fetch",
        source_id=source_id,
        state=TaskState.RUNNING,
        locked_by=worker,
        locked_at=datetime.now(UTC) - since,
        attempts=attempts,
    )


def test_complete_marks_done_and_commits_with_the_handler_results(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id))

    with Session(engine) as session:
        task = claim(session, kinds=["fetch"], worker="w1")
        assert task is not None
        child = Task(kind="extract", source_id=queue_source_id)
        session.add(child)
        complete(session, task_id, worker="w1")
        session.commit()
        child_id = child.id

    with Session(engine) as other:
        done = other.get_one(Task, task_id)
        assert done.state == TaskState.DONE
        assert done.locked_by is None
        assert done.locked_at is None
        assert other.get(Task, child_id) is not None


def test_complete_after_the_lease_was_reaped_raises_and_commits_nothing(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id))

    with Session(engine) as slow:
        claim(slow, kinds=["fetch"], worker="slow")
        with Session(engine) as reaper:
            assert reap(reaper, lease=timedelta(0)) == 1
            reaper.commit()

        slow.add(Task(kind="extract", source_id=queue_source_id))
        with pytest.raises(LeaseLost):
            complete(slow, task_id, worker="slow")
        slow.rollback()

    with Session(engine) as other:
        assert other.get_one(Task, task_id).state == TaskState.FAILED
        kinds = other.scalars(select(Task.kind)).all()
        assert kinds == ["fetch"]


def test_complete_by_a_worker_that_does_not_hold_the_lease_raises(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(_running(queue_source_id, worker="w1", since=timedelta(0)))

    with Session(engine) as session, pytest.raises(LeaseLost):
        complete(session, task_id, worker="w2")


def test_fail_schedules_a_retry_after_the_backoff(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id))

    with Session(engine) as session:
        claim(session, kinds=["fetch"], worker="w1")
        fail(session, task_id, worker="w1", error="ConnectTimeout: timed out")
        session.commit()

    with Session(engine) as other:
        task = other.get_one(Task, task_id)
        assert task.state == TaskState.FAILED
        assert task.last_error == "ConnectTimeout: timed out"
        assert task.locked_by is None
        assert task.locked_at is None
        # run_after and updated_at are both set from the same transaction's now().
        assert task.run_after - task.updated_at == backoff(1)


def test_fail_on_the_last_attempt_marks_the_task_dead(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(
        Task(kind="fetch", source_id=queue_source_id, attempts=MAX_ATTEMPTS - 1)
    )

    with Session(engine) as session:
        claim(session, kinds=["fetch"], worker="w1")
        fail(session, task_id, worker="w1", error="HTTPStatusError: 503")
        session.commit()

    with Session(engine) as other:
        task = other.get_one(Task, task_id)
        assert task.state == TaskState.DEAD
        assert task.attempts == MAX_ATTEMPTS
        assert task.last_error == "HTTPStatusError: 503"


def test_fail_by_a_worker_that_does_not_hold_the_lease_raises(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(_running(queue_source_id, worker="w1", since=timedelta(0)))

    with Session(engine) as session, pytest.raises(LeaseLost):
        fail(session, task_id, worker="w2", error="x")


def test_backoff_doubles_with_each_attempt() -> None:
    assert [backoff(n) / backoff(1) for n in range(1, MAX_ATTEMPTS)] == [
        2**k for k in range(MAX_ATTEMPTS - 1)
    ]


def test_reap_requeues_only_expired_leases(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    expired, fresh = add_tasks(
        _running(queue_source_id, worker="killed", since=timedelta(hours=1)),
        _running(queue_source_id, worker="alive", since=timedelta(seconds=5)),
    )

    with Session(engine) as session:
        assert reap(session, lease=timedelta(minutes=10)) == 1
        session.commit()

    with Session(engine) as other:
        reaped = other.get_one(Task, expired)
        assert reaped.state == TaskState.FAILED
        assert reaped.locked_by is None
        assert reaped.last_error is not None
        assert other.get_one(Task, fresh).state == TaskState.RUNNING


def test_reap_marks_a_task_that_keeps_crashing_its_worker_dead(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    (task_id,) = add_tasks(
        _running(
            queue_source_id, worker="killed", since=timedelta(hours=1), attempts=MAX_ATTEMPTS
        ),
    )

    with Session(engine) as session:
        reap(session, lease=timedelta(minutes=10))
        session.commit()

    with Session(engine) as other:
        assert other.get_one(Task, task_id).state == TaskState.DEAD
