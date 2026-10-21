"""The per-host circuit breaker (ADR 0030 §5), on ops.host.circuit_state.

CIRCUIT_THRESHOLD consecutive failed attempts against a host open its circuit and push its
next_allowed_at out by CIRCUIT_COOLDOWN, so the host's tasks wait without spending attempts.
After the cool-down one claim is the probe (half_open, no second claim); the probe's success
closes the circuit, its failure or loss re-opens it. These tests really commit.
"""

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from aoi.db.schema.enums import CircuitState, TaskState
from aoi.db.schema.ops import Host, Task
from aoi.tasks.queue import (
    CIRCUIT_COOLDOWN,
    CIRCUIT_THRESHOLD,
    claim,
    complete,
    fail,
    reap,
)

pytestmark = pytest.mark.integration

AddTasks = Callable[..., list[uuid.UUID]]
AddHost = Callable[..., uuid.UUID]


def _host(engine: Engine, host_id: uuid.UUID) -> Host:
    with Session(engine) as session:
        host = session.get_one(Host, host_id)
        session.expunge(host)
        return host


def _claim_and_fail(engine: Engine) -> uuid.UUID:
    with Session(engine) as session:
        task = claim(session, kinds=["fetch"], worker="w1")
        assert task is not None
        task_id = task.id
        fail(session, task_id, worker="w1", error="ConnectError: connection refused")
        session.commit()
    return task_id


def test_a_failure_below_the_threshold_counts_but_keeps_the_circuit_closed(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    host_id = add_host()
    add_tasks(Task(kind="fetch", source_id=queue_source_id, host_id=host_id))

    _claim_and_fail(engine)

    host = _host(engine, host_id)
    assert host.circuit_state == CircuitState.CLOSED
    assert host.consecutive_failures == 1


def test_the_threshold_failure_opens_the_circuit_and_holds_the_hosts_tasks(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    host_id = add_host(consecutive_failures=CIRCUIT_THRESHOLD - 1)
    _, waiting = add_tasks(
        Task(kind="fetch", source_id=queue_source_id, host_id=host_id, priority=10),
        Task(kind="fetch", source_id=queue_source_id, host_id=host_id),
    )

    _claim_and_fail(engine)

    host = _host(engine, host_id)
    assert host.circuit_state == CircuitState.OPEN
    # Both are set from the failing transaction's now().
    assert host.next_allowed_at - host.updated_at == CIRCUIT_COOLDOWN
    with Session(engine) as session:
        assert claim(session, kinds=["fetch"], worker="w2") is None
        assert session.get_one(Task, waiting).attempts == 0


def test_a_success_resets_the_failure_count(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    host_id = add_host(consecutive_failures=3)
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id, host_id=host_id))

    with Session(engine) as session:
        claim(session, kinds=["fetch"], worker="w1")
        complete(session, task_id, worker="w1")
        session.commit()

    host = _host(engine, host_id)
    assert host.circuit_state == CircuitState.CLOSED
    assert host.consecutive_failures == 0


def test_after_the_cooldown_exactly_one_probe_is_claimed(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    host_id = add_host(
        circuit_state=CircuitState.OPEN,
        consecutive_failures=CIRCUIT_THRESHOLD,
        next_allowed_at=datetime.now(UTC) - timedelta(seconds=1),
        rps="500",
    )
    add_tasks(*(Task(kind="fetch", source_id=queue_source_id, host_id=host_id) for _ in range(2)))

    with Session(engine) as session:
        assert claim(session, kinds=["fetch"], worker="w1") is not None
        assert _host(engine, host_id).circuit_state == CircuitState.HALF_OPEN
        time.sleep(0.01)  # 500 rps: the rate allows another request after 2 ms
        assert claim(session, kinds=["fetch"], worker="w2") is None, "a second probe was claimed"


def _probe(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> tuple[uuid.UUID, uuid.UUID]:
    """A half-open host whose probe task w1 has claimed."""
    host_id = add_host(
        circuit_state=CircuitState.OPEN,
        consecutive_failures=CIRCUIT_THRESHOLD,
        next_allowed_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    (task_id,) = add_tasks(Task(kind="fetch", source_id=queue_source_id, host_id=host_id))
    with Session(engine) as session:
        assert claim(session, kinds=["fetch"], worker="w1") is not None
    return host_id, task_id


def test_a_successful_probe_closes_the_circuit(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    host_id, task_id = _probe(engine, queue_source_id, add_tasks, add_host)

    with Session(engine) as session:
        complete(session, task_id, worker="w1")
        session.commit()

    host = _host(engine, host_id)
    assert host.circuit_state == CircuitState.CLOSED
    assert host.consecutive_failures == 0


def test_a_failed_probe_reopens_the_circuit(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    host_id, task_id = _probe(engine, queue_source_id, add_tasks, add_host)

    with Session(engine) as session:
        fail(session, task_id, worker="w1", error="ReadTimeout: timed out")
        session.commit()

    host = _host(engine, host_id)
    assert host.circuit_state == CircuitState.OPEN
    assert host.next_allowed_at - host.updated_at == CIRCUIT_COOLDOWN


def test_a_probe_lost_with_its_worker_reopens_the_circuit(
    engine: Engine, queue_source_id: uuid.UUID, add_tasks: AddTasks, add_host: AddHost
) -> None:
    host_id, task_id = _probe(engine, queue_source_id, add_tasks, add_host)

    with Session(engine) as session:
        assert reap(session, lease=timedelta(0)) == 1
        session.commit()

    assert _host(engine, host_id).circuit_state == CircuitState.OPEN
    with Session(engine) as session:
        assert session.get_one(Task, task_id).state == TaskState.FAILED
