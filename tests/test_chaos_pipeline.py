"""End-to-end chaos test (plan §4.3, M2 exit): killing workers loses and duplicates nothing.

Three real worker processes (tests/chaos_pipeline_worker.py) run the real fetch, extract,
resolve and lifecycle handlers over ten listing targets; one listing links to two more pages,
which extraction discovers and the scheduler queues. Leaseholders are SIGKILLed four times (fewer
than MAX_ATTEMPTS, so no task may legitimately die). The test runs the scheduler's tick itself, and
makes failed tasks due at once instead of after the real backoff.

Expected: every target fetched exactly once and every fetch extracted exactly once, one posting
per job with exactly one history version, nothing left open. WARC objects orphaned by a kill are
allowed (ADR 0030 §2) and not counted.
"""

import os
import random
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from aoi.db.schema.core import Assertion, ExtractionRun, Posting, PostingHistory, Source
from aoi.db.schema.enums import LegalBasis, TaskState
from aoi.db.schema.evidence import Fetch
from aoi.db.schema.ops import CrawlTarget, Host, Task
from aoi.tasks.queue import MAX_ATTEMPTS
from aoi.tasks.scheduler import tick

pytestmark = pytest.mark.integration

AddTarget = Callable[..., uuid.UUID]
WORKER = Path(__file__).parent / "chaos_pipeline_worker.py"
HOST = "queue-test-e2e.example.org"
LISTINGS = 10
LEASE = timedelta(seconds=2)
_OPEN = (TaskState.QUEUED, TaskState.RUNNING, TaskState.FAILED)


def _count(session: Session, model: type, *where: object) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where)) or 0


def _scheduler_pass(engine: Engine) -> int:
    """One tick, failed tasks made due now; returns how many tasks are still open."""
    with Session(engine) as session, session.begin():
        tick(session, lease=LEASE)
        session.execute(
            update(Task).where(Task.state == TaskState.FAILED).values(run_after=func.now())
        )
    with Session(engine) as session:
        return _count(session, Task, Task.state.in_(_OPEN))


def _leaseholders(engine: Engine) -> set[int]:
    with Session(engine) as session:
        holders = session.scalars(select(Task.locked_by).where(Task.state == TaskState.RUNNING))
        return {int(h.split(":")[1]) for h in holders if h is not None}


def test_killing_pipeline_workers_mid_task_loses_and_duplicates_nothing(
    engine: Engine, queue_source_id: uuid.UUID, add_target: AddTarget
) -> None:
    with Session(engine) as session, session.begin():
        source = session.get_one(Source, queue_source_id)
        source.enabled, source.legal_basis = True, LegalBasis.OFFICIAL_API
        name = source.name
        session.add(Host(hostname=HOST, rps=Decimal("500")))
    for listing in range(LISTINGS):
        add_target(url=f"https://{HOST}/api/jobs?list={listing}&page=1")

    env = {**os.environ, "CHAOS_SOURCE": name}

    def spawn() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, str(WORKER)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    rng = random.Random(20261018)
    workers = {w.pid: w for w in (spawn() for _ in range(3))}
    kills = 0
    deadline = time.monotonic() + 90
    try:
        while time.monotonic() < deadline:
            open_tasks = _scheduler_pass(engine)
            busy = sorted(_leaseholders(engine) & workers.keys())
            if kills < MAX_ATTEMPTS - 1 and busy:
                victim = workers.pop(rng.choice(busy))
                # A lease is first seen just after the claim; killing at once would almost never
                # land between a handler's writes and its commit. Spread kills over a whole task.
                time.sleep(rng.uniform(0, 0.2))
                victim.kill()
                victim.wait()
                kills += 1
                replacement = spawn()
                workers[replacement.pid] = replacement
            elif kills == MAX_ATTEMPTS - 1 and open_tasks == 0:
                with Session(engine) as session:
                    if _count(session, CrawlTarget) == LISTINGS + 2:
                        break
            time.sleep(0.05)
    finally:
        for worker in workers.values():
            worker.kill()
            worker.wait()

    with Session(engine) as session:
        targets = session.scalars(select(CrawlTarget.id)).all()
        fetches_per_target = {
            t: _count(session, Fetch, Fetch.crawl_target_id == t, Fetch.http_status == 200)
            for t in targets
        }
        fetch_ids = session.scalars(select(Fetch.id)).all()
        runs_per_fetch = {
            f: _count(session, ExtractionRun, ExtractionRun.fetch_id == f) for f in fetch_ids
        }
        states = session.scalars(select(Task.state)).all()
        attempts = session.scalars(select(Task.attempts)).all()
        postings = _count(session, Posting)
        assertions = _count(session, Assertion)
        versions = _count(session, PostingHistory)

    assert kills == MAX_ATTEMPTS - 1
    assert len(targets) == LISTINGS + 2, "a discovered page was lost"
    assert set(fetches_per_target.values()) == {1}, f"fetched zero or twice: {fetches_per_target}"
    assert set(runs_per_fetch.values()) == {1}, f"extracted zero or twice: {runs_per_fetch}"
    assert set(states) == {TaskState.DONE}, f"tasks left behind: {states}"
    assert postings == assertions == versions == 3 * (LISTINGS + 2)
    assert max(attempts) > 1, "no kill landed mid-task; the test proved nothing"
