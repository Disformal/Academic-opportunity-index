"""The worker loop (ADR 0014, 0030): claim a task, run its handler, commit or record why not."""

import signal
import threading
from collections.abc import Callable, Mapping

import structlog
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from aoi.db.schema.ops import Task
from aoi.tasks.queue import LeaseLost, claim, complete, fail

log = structlog.get_logger()

Handler = Callable[[Session, Task], None]
"""Does a task's work in the given session. It never commits: the worker commits or rolls back."""


class AttemptFailed(Exception):
    """The attempt failed, but the handler's writes record it (e.g. a failed request's fetch row).

    The worker commits those writes together with the failure; any other exception rolls them back.
    """


def run_once(engine: Engine, handlers: Mapping[str, Handler], *, worker: str) -> bool:
    """Claim one due task of a kind in `handlers` and run it; False if none was due."""
    with Session(engine) as session:
        task = claim(session, kinds=list(handlers), worker=worker)
        if task is None:
            return False
        task_id = task.id
        try:
            handlers[task.kind](session, task)
            complete(session, task_id, worker=worker)
            session.commit()
        except LeaseLost:
            session.rollback()
            log.warning("task.result_discarded", task_id=str(task_id), worker=worker)
        except Exception as exc:  # every handler error is recorded on the task, never lost
            if not isinstance(exc, AttemptFailed):
                session.rollback()
            try:
                fail(session, task_id, worker=worker, error=f"{type(exc).__name__}: {exc}")
                session.commit()
            except LeaseLost:
                session.rollback()
                log.warning("task.failure_discarded", task_id=str(task_id), worker=worker)
    return True


def run(
    engine: Engine,
    handlers: Mapping[str, Handler],
    *,
    worker: str,
    should_stop: Callable[[], bool],
    idle: Callable[[], None],
) -> None:
    """Run tasks until `should_stop()`; call `idle()` (e.g. a short sleep) when none is due."""
    log.info("worker.started", worker=worker, kinds=sorted(handlers))
    while not should_stop():
        if not run_once(engine, handlers, worker=worker):
            idle()
    log.info("worker.stopped", worker=worker)


def stop_on_signals() -> threading.Event:
    """An event set by SIGTERM or SIGINT, so a loop can finish its current step and stop."""
    stop = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        log.info("process.stop_requested", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return stop


def serve(
    engine: Engine, handlers: Mapping[str, Handler], *, worker: str, idle_seconds: float
) -> None:
    """Run tasks until SIGTERM or SIGINT; the task in hand is always finished first."""
    stop = stop_on_signals()

    def idle() -> None:
        stop.wait(idle_seconds)  # returns early when a stop is requested

    run(engine, handlers, worker=worker, should_stop=stop.is_set, idle=idle)
