"""Host politeness in the claim (plan §4.3, ADR 0014): a task that contacts a host is claimable
only when the host's next_allowed_at has passed, and claiming it moves next_allowed_at forward by
1/rps in the same transaction, so all workers together respect the host's rate.

These tests really commit.
"""

import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from aoi.db.schema.ops import Host, Task
from aoi.tasks.queue import claim

pytestmark = pytest.mark.integration

AddTasks = Callable[..., list[uuid.UUID]]
AddHost = Callable[..., uuid.UUID]


def test_a_task_is_not_claimable_while_its_host_is_not_due(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    host = add_host(next_allowed_at=datetime.now(UTC) + timedelta(hours=1))
    add_tasks(Task(kind="fetch", source_id=queue_source_id, host_id=host))

    with Session(engine) as session:
        assert claim(session, kinds=["fetch"], worker="w1") is None


def test_claim_moves_the_host_forward_by_one_over_rps(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    host_id = add_host(rps="2")
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id, host_id=host_id))

    with Session(engine) as session:
        assert claim(session, kinds=["fetch"], worker="w1") is not None

    with Session(engine) as other:
        task = other.get_one(Task, task_id)
        host = other.get_one(Host, host_id)
        assert task.locked_at is not None
        # Both are set from the claim transaction's now().
        assert host.next_allowed_at - task.locked_at == timedelta(seconds=0.5)


def test_a_second_task_for_the_same_host_waits_while_other_hosts_proceed(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    busy, other_host = add_host(), add_host()
    first, second, elsewhere = add_tasks(
        Task(kind="fetch", source_id=queue_source_id, host_id=busy, priority=10),
        Task(kind="fetch", source_id=queue_source_id, host_id=busy, priority=10),
        Task(kind="fetch", source_id=queue_source_id, host_id=other_host),
    )

    claimed = []
    with Session(engine) as session:
        while (task := claim(session, kinds=["fetch"], worker="w1")) is not None:
            claimed.append(task.id)

    assert len(claimed) == 2
    assert claimed[1] == elsewhere
    assert {first, second} - set(claimed) != set()


def test_a_task_without_a_host_is_not_held_back(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    host = add_host(next_allowed_at=datetime.now(UTC) + timedelta(hours=1))
    _, hostless = add_tasks(
        Task(kind="extract", source_id=queue_source_id, host_id=host, priority=10),
        Task(kind="extract", source_id=queue_source_id),
    )

    with Session(engine) as session:
        task = claim(session, kinds=["extract"], worker="w1")

    assert task is not None
    assert task.id == hostless


def test_concurrent_claimers_never_exceed_the_host_rate(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    host_id = add_host(rps="20")
    ids = add_tasks(
        *(Task(kind="fetch", source_id=queue_source_id, host_id=host_id) for _ in range(12))
    )
    claimed: list[uuid.UUID] = []

    def drain(worker: str) -> None:
        with Session(engine) as session:
            while len(claimed) < len(ids):
                task = claim(session, kinds=["fetch"], worker=worker)
                if task is None:
                    time.sleep(0.005)
                else:
                    claimed.append(task.id)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(drain, [f"w{i}" for i in range(6)]))

    with Session(engine) as session:
        times = sorted(session.scalars(select(Task.locked_at).where(Task.host_id == host_id)))
    gaps = [b - a for a, b in zip(times, times[1:], strict=False) if a and b]
    assert sorted(claimed) == sorted(ids)
    assert min(gaps) >= timedelta(seconds=0.05)
