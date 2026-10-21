"""The scheduler (plan §4.3): reap expired leases, then queue the work that is due.

Only enabled sources with a legal basis are crawled; enabled sources without one are refused and
logged on every tick (ADR 0012). A due target that is not retired gets a fetch task unless one is
already open (the partial unique index uq_task_open_target backs this up), and its next regular
time is set now: a task that fails, however it fails, does not bring the target back before then,
so a dead task waits for review instead of being re-queued every tick (plan §4.3). A source with no
targets that was never discovered gets one discover task; rediscovery is a deliberate act, not a
loop. Each eligible source gets a lifecycle task (ADR 0033) when none is open and none was queued
in the last hour.
"""

from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import Engine, func, or_, select
from sqlalchemy.orm import Session

from aoi.db.schema.core import Source
from aoi.db.schema.ops import CrawlTarget, Task
from aoi.tasks.queue import OPEN_STATES, reap
from aoi.tasks.worker import stop_on_signals

log = structlog.get_logger()

LIFECYCLE_EVERY = timedelta(hours=1)


@dataclass(frozen=True)
class Tick:
    reaped: int
    fetches: int
    discovers: int
    lifecycles: int
    refused: tuple[str, ...]


def tick(session: Session, *, lease: timedelta) -> Tick:
    """One scheduler pass in the caller's transaction; the caller commits."""
    reaped = reap(session, lease=lease)

    refused = tuple(
        session.scalars(select(Source.name).where(Source.enabled, Source.legal_basis.is_(None)))
    )
    for name in refused:
        log.warning("scheduler.refused", source=name, reason="no legal basis (ADR 0012)")
    eligible = select(Source.id).where(Source.enabled, Source.legal_basis.is_not(None))

    open_fetch = (
        select(Task.id)
        .where(
            Task.kind == "fetch",
            Task.crawl_target_id == CrawlTarget.id,
            Task.state.in_(OPEN_STATES),
        )
        .exists()
    )
    due = session.scalars(
        select(CrawlTarget).where(
            CrawlTarget.source_id.in_(eligible),
            CrawlTarget.next_due_at <= func.now(),
            CrawlTarget.retired_at.is_(None),
            ~open_fetch,
        )
    ).all()
    for t in due:
        t.next_due_at = func.now() + t.recrawl_interval
        session.add(
            Task(kind="fetch", source_id=t.source_id, host_id=t.host_id, crawl_target_id=t.id)
        )

    has_targets = select(CrawlTarget.id).where(CrawlTarget.source_id == Source.id).exists()
    was_discovered = (
        select(Task.id).where(Task.kind == "discover", Task.source_id == Source.id).exists()
    )
    undiscovered = session.scalars(
        select(Source.id).where(Source.id.in_(eligible), ~has_targets, ~was_discovered)
    ).all()
    session.add_all(Task(kind="discover", source_id=source_id) for source_id in undiscovered)

    recent_lifecycle = (
        select(Task.id)
        .where(
            Task.kind == "lifecycle",
            Task.source_id == Source.id,
            or_(Task.state.in_(OPEN_STATES), Task.created_at > func.now() - LIFECYCLE_EVERY),
        )
        .exists()
    )
    lifecycle_due = session.scalars(
        select(Source.id).where(Source.id.in_(eligible), ~recent_lifecycle)
    ).all()
    session.add_all(Task(kind="lifecycle", source_id=source_id) for source_id in lifecycle_due)

    session.flush()
    report = Tick(
        reaped=reaped,
        fetches=len(due),
        discovers=len(undiscovered),
        lifecycles=len(lifecycle_due),
        refused=refused,
    )
    log.info(
        "scheduler.tick",
        reaped=report.reaped,
        fetches=report.fetches,
        discovers=report.discovers,
        lifecycles=report.lifecycles,
        refused=len(report.refused),
    )
    return report


def serve_scheduler(
    engine: Engine, *, every: timedelta, lease: timedelta, once: bool = False
) -> None:
    """Tick every `every`, committing each tick, until SIGTERM or SIGINT; once: one tick."""
    stop = stop_on_signals()
    while True:
        with Session(engine) as session, session.begin():
            tick(session, lease=lease)
        if once or stop.wait(every.total_seconds()):
            return
