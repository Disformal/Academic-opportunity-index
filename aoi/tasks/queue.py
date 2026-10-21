"""ops.task under a lease (ADR 0030): claim, complete, fail, reap expired leases, and requeue a
dead task after review.

`claim` commits its own short transaction. `complete`, `fail`, `reap` and `requeue` join the
caller's transaction and leave the commit to it, so a handler's results and its `done` commit
together.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta

import structlog
from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.orm import Session

from aoi.db.schema.core import Source
from aoi.db.schema.enums import CircuitState, TaskState
from aoi.db.schema.ops import Host, Task

log = structlog.get_logger()

MAX_ATTEMPTS = 5
_BACKOFF_BASE = timedelta(minutes=5)
CIRCUIT_THRESHOLD = 5
CIRCUIT_COOLDOWN = timedelta(minutes=15)
OPEN_STATES = (TaskState.QUEUED, TaskState.RUNNING, TaskState.FAILED)


class LeaseLost(Exception):
    """The caller no longer holds the task's lease; it must roll back its work."""


class NotRequeueable(Exception):
    """The task is not a dead task that may be retried now."""


def backoff(attempts: int) -> timedelta:
    """Delay before the retry that follows attempt number `attempts`: 5, 10, 20, 40 min."""
    factor: int = 2 ** (attempts - 1)
    return _BACKOFF_BASE * factor


def claim(session: Session, *, kinds: Sequence[str], worker: str) -> Task | None:
    """Claim the next due task of one of `kinds` and commit the lease.

    SKIP LOCKED lets concurrent claimers pass over each other's candidate rows. A task with a
    host also takes that host's next request slot in the same transaction (plan §4.3); on a host
    whose circuit is open and cooled down, that task is the single probe. The attempt is counted
    here, not on completion, so a task that crashes its worker still runs out.
    """
    busy_hosts: set[uuid.UUID] = set()
    while True:
        task = session.scalars(_next_candidate(kinds, busy_hosts)).first()
        if task is None:
            session.rollback()
            return None
        if task.host_id is None or _take_host_slot(session, task.host_id):
            break
        # Another claimer holds the host's slot, or it just took it: try a different host.
        busy_hosts.add(task.host_id)
        session.rollback()

    task.state = TaskState.RUNNING
    task.locked_by = worker
    task.locked_at = func.now()
    task.attempts += 1
    session.commit()
    log.info("task.claimed", task_id=str(task.id), kind=task.kind, worker=worker)
    return task


def _next_candidate(kinds: Sequence[str], busy_hosts: set[uuid.UUID]) -> Select[Task]:
    due_hosts = select(Host.id).where(
        Host.next_allowed_at <= func.now(), Host.circuit_state != CircuitState.HALF_OPEN
    )
    query = select(Task).where(
        Task.kind.in_(kinds),
        Task.state.in_([TaskState.QUEUED, TaskState.FAILED]),
        Task.run_after <= func.now(),
        or_(Task.host_id.is_(None), Task.host_id.in_(due_hosts)),
    )
    if busy_hosts:
        query = query.where(or_(Task.host_id.is_(None), Task.host_id.not_in(busy_hosts)))
    return (
        query.order_by(Task.priority.desc(), Task.run_after)
        .limit(1)
        .with_for_update(of=Task, skip_locked=True)
    )


def _take_host_slot(session: Session, host_id: uuid.UUID) -> bool:
    """Lock the host if it is due and move next_allowed_at to now() + 1/rps.

    An open circuit whose cool-down has passed turns half_open: this claim is its probe, and no
    other task is claimed for the host until the probe reports.
    """
    host = session.scalars(
        select(Host)
        .where(
            Host.id == host_id,
            Host.next_allowed_at <= func.now(),
            Host.circuit_state != CircuitState.HALF_OPEN,
        )
        .with_for_update(skip_locked=True)
    ).first()
    if host is None:
        return False
    if host.circuit_state == CircuitState.OPEN:
        host.circuit_state = CircuitState.HALF_OPEN
        log.info("host.circuit_probe", host=host.hostname)
    host.next_allowed_at = func.now() + timedelta(seconds=1 / float(host.rps))
    return True


def _host_succeeded(session: Session, host_id: uuid.UUID) -> None:
    # Conditional, so the usual case (already closed, no failures) writes and locks nothing.
    session.execute(
        update(Host)
        .where(
            Host.id == host_id,
            or_(Host.consecutive_failures > 0, Host.circuit_state != CircuitState.CLOSED),
        )
        .values(consecutive_failures=0, circuit_state=CircuitState.CLOSED)
    )


def _host_failed(session: Session, host_id: uuid.UUID) -> None:
    host = session.scalars(select(Host).where(Host.id == host_id).with_for_update()).one()
    host.consecutive_failures += 1
    if (
        host.circuit_state == CircuitState.HALF_OPEN
        or host.consecutive_failures >= CIRCUIT_THRESHOLD
    ):
        host.circuit_state = CircuitState.OPEN
        host.next_allowed_at = func.now() + CIRCUIT_COOLDOWN
        log.warning("host.circuit_open", host=host.hostname, failures=host.consecutive_failures)


def _held(session: Session, task_id: uuid.UUID, worker: str) -> Task:
    """Lock the task if `worker` still holds its lease; otherwise raise LeaseLost."""
    task = session.scalars(
        select(Task)
        .where(Task.id == task_id, Task.state == TaskState.RUNNING, Task.locked_by == worker)
        .with_for_update()
    ).first()
    if task is None:
        raise LeaseLost(f"task {task_id} is not leased to {worker}")
    return task


def _record_failure(session: Session, task: Task, error: str) -> None:
    if task.host_id is not None:
        _host_failed(session, task.host_id)
    task.locked_by = None
    task.locked_at = None
    task.last_error = error
    if task.attempts >= MAX_ATTEMPTS:
        task.state = TaskState.DEAD
    else:
        task.state = TaskState.FAILED
        task.run_after = func.now() + backoff(task.attempts)


def complete(session: Session, task_id: uuid.UUID, *, worker: str) -> None:
    """Mark the task done in the caller's transaction, alongside the handler's results."""
    task = _held(session, task_id, worker)
    if task.host_id is not None:
        _host_succeeded(session, task.host_id)
    task.state = TaskState.DONE
    task.locked_by = None
    task.locked_at = None
    session.flush()
    log.info("task.done", task_id=str(task_id), kind=task.kind, worker=worker)


def fail(session: Session, task_id: uuid.UUID, *, worker: str, error: str) -> None:
    """Record a failed attempt: retry after the backoff, or `dead` at the attempt limit.

    Call it after rolling back the handler's work.
    """
    task = _held(session, task_id, worker)
    _record_failure(session, task, error)
    session.flush()
    log.warning("task.failed", task_id=str(task_id), kind=task.kind, state=task.state, error=error)


def reap(session: Session, *, lease: timedelta) -> int:
    """Treat every lease older than `lease` as a failed attempt; return how many were reaped."""
    tasks = session.scalars(
        select(Task)
        .where(Task.state == TaskState.RUNNING, Task.locked_at < func.now() - lease)
        .with_for_update(skip_locked=True)
    ).all()
    for task in tasks:
        log.warning("task.lease_expired", task_id=str(task.id), worker=task.locked_by)
        _record_failure(
            session, task, f"LeaseExpired: no result from {task.locked_by} within {lease}"
        )
    session.flush()
    return len(tasks)


def requeue(session: Session, task_id: uuid.UUID) -> Task:
    """Queue a fresh copy of a dead task after review, linked to it by `retry_of`.

    The dead row is never changed, so its failure stays in the record and in past run
    accounting. Refused when the task is not dead or was requeued before, when the scheduler
    would refuse its source (disabled, or no legal basis: ADR 0012), or when an open task already
    does the same work (the partial unique indexes on ops.task would reject the copy).
    """
    dead = session.scalars(
        select(Task).where(Task.id == task_id, Task.state == TaskState.DEAD).with_for_update()
    ).first()
    if dead is None:
        raise NotRequeueable(f"task {task_id} is not a dead task")
    if session.scalars(select(Task.id).where(Task.retry_of == task_id)).first() is not None:
        raise NotRequeueable(f"task {task_id} was requeued before")
    source = session.get_one(Source, dead.source_id)
    if not source.enabled or source.legal_basis is None:
        raise NotRequeueable(f"source {source.name} is disabled or has no legal basis")
    same_work = []
    if dead.crawl_target_id is not None:
        same_work.append(Task.crawl_target_id == dead.crawl_target_id)
    if dead.kind == "discover":
        same_work.append(Task.source_id == dead.source_id)
    if same_work:
        twin = session.scalars(
            select(Task.id).where(
                Task.kind == dead.kind, Task.state.in_(OPEN_STATES), or_(*same_work)
            )
        ).first()
        if twin is not None:
            raise NotRequeueable(f"task {twin} already does the same work")

    retry = Task(
        kind=dead.kind,
        source_id=dead.source_id,
        crawl_target_id=dead.crawl_target_id,
        host_id=dead.host_id,
        payload=dead.payload,
        priority=dead.priority,
        retry_of=dead.id,
    )
    session.add(retry)
    session.flush()
    log.info("task.requeued", task_id=str(retry.id), retry_of=str(task_id), kind=dead.kind)
    return retry
