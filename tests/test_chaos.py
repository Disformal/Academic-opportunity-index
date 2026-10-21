"""Chaos test (plan §4.3, M2 exit): killing workers mid-task loses and duplicates nothing.

Three real worker processes (tests/chaos_worker.py) drain 30 tasks, each of which writes one
child task and then holds its transaction open. Workers are killed with SIGKILL at random
and replaced. Fewer kills than MAX_ATTEMPTS means no task can legitimately end up dead.
"""

import os
import random
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from aoi.db.schema.enums import TaskState
from aoi.db.schema.ops import Task
from aoi.tasks.queue import MAX_ATTEMPTS, reap

pytestmark = pytest.mark.integration

AddTasks = Callable[..., list[uuid.UUID]]

CHAOS_WORKER = Path(__file__).parent / "chaos_worker.py"
LEASE = timedelta(seconds=2)


def _spawn() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, str(CHAOS_WORKER)],
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap_and_fast_forward(engine: Engine) -> int:
    """Reap expired leases, then make failed tasks due now instead of after the real backoff."""
    with Session(engine) as session:
        reap(session, lease=LEASE)
        session.execute(
            update(Task).where(Task.state == TaskState.FAILED).values(run_after=func.now())
        )
        session.commit()
        return (
            session.scalar(
                select(func.count()).where(Task.kind == "chaos", Task.state != TaskState.DONE)
            )
            or 0
        )


def _leaseholders(engine: Engine) -> set[int]:
    """PIDs of workers holding a lease right now, i.e. stopped between claim and commit."""
    with Session(engine) as session:
        holders = session.scalars(
            select(Task.locked_by).where(Task.kind == "chaos", Task.state == TaskState.RUNNING)
        )
        return {int(h.split(":")[1]) for h in holders if h is not None}


def test_killing_workers_mid_task_loses_and_duplicates_nothing(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks
) -> None:
    ids = add_tasks(*(Task(kind="chaos", source_id=queue_source_id) for _ in range(30)))
    rng = random.Random(20261018)
    workers = {w.pid: w for w in (_spawn() for _ in range(3))}
    kills = 0
    deadline = time.monotonic() + 60
    try:
        # Kill only leaseholders: a fresh process takes longer to start than a task takes to
        # run, so random victims are mostly still booting and no kill lands mid-task.
        while kills < MAX_ATTEMPTS - 1 and time.monotonic() < deadline:
            busy = sorted(_leaseholders(engine) & workers.keys())
            if not busy:
                time.sleep(0.02)
                continue
            victim = workers.pop(rng.choice(busy))
            victim.kill()
            victim.wait()
            kills += 1
            replacement = _spawn()
            workers[replacement.pid] = replacement
        while _reap_and_fast_forward(engine) > 0 and time.monotonic() < deadline:
            time.sleep(0.2)
    finally:
        for worker in workers.values():
            worker.kill()
            worker.wait()

    with Session(engine) as session:
        parents = session.scalars(select(Task).where(Task.kind == "chaos")).all()
        children = session.scalars(select(Task.payload).where(Task.kind == "child")).all()

    assert kills == MAX_ATTEMPTS - 1
    assert {t.state for t in parents} == {TaskState.DONE}, "a task was lost or left behind"
    assert sorted(c["parent"] for c in children) == sorted(str(i) for i in ids), (
        "a task's result was lost or written twice"
    )
    assert any(t.attempts > 1 for t in parents), "no kill landed mid-task; the test proved nothing"
