"""AOI command-line interface."""

import argparse
import sys
import uuid
from pathlib import Path

import structlog

from aoi.db.session import db_session

log = structlog.get_logger()


def _ror_sync(args: argparse.Namespace) -> None:
    from aoi.reference.ror import sync_ror

    with db_session() as session:
        sync_ror(session, Path(args.file))


def _taxonomy_sync(args: argparse.Namespace) -> None:
    from aoi.reference.taxonomy import sync_taxonomy

    with db_session() as session:
        sync_taxonomy(session, Path(args.file))


def _worker(args: argparse.Namespace) -> None:
    import os
    import socket

    from aoi.adapters.registry import ADAPTERS
    from aoi.db.session import get_engine
    from aoi.tasks.handlers import handlers_for
    from aoi.tasks.worker import serve

    serve(
        get_engine(),
        handlers_for(args.kinds.split(","), ADAPTERS),
        worker=f"{socket.gethostname()}:{os.getpid()}",
        idle_seconds=args.idle,
    )


def _scheduler(args: argparse.Namespace) -> None:
    from datetime import timedelta

    from aoi.db.session import get_engine
    from aoi.tasks.scheduler import serve_scheduler

    serve_scheduler(
        get_engine(),
        every=timedelta(seconds=args.every),
        lease=timedelta(seconds=args.lease),
        once=args.once,
    )


def _replay(args: argparse.Namespace) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from aoi.adapters.registry import ADAPTERS
    from aoi.db.schema.core import Source
    from aoi.db.session import get_engine
    from aoi.tasks.replay import queue_replay

    with Session(get_engine()) as session, session.begin():
        source = session.scalars(select(Source).where(Source.name == args.source)).first()
        if source is None:
            sys.exit(f"no source named {args.source!r}")
        adapter = ADAPTERS.get(source.platform or source.name)
        if adapter is None:
            sys.exit(f"no adapter registered for source {args.source!r}")
        queue_replay(session, source, adapter)


def _lifecycle(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from aoi.db.schema.core import Source
    from aoi.db.session import get_engine
    from aoi.tasks.handlers.lifecycle import MassClosureHeld, evaluate

    try:
        with Session(get_engine()) as session, session.begin():
            source = session.scalars(select(Source).where(Source.name == args.source)).first()
            if source is None:
                sys.exit(f"no source named {args.source!r}")
            evaluate(
                session,
                source.id,
                now=datetime.now(UTC),
                allow_mass_closure=args.allow_mass_closure,
            )
    except MassClosureHeld as exc:
        sys.exit(f"held: {exc}")


def _target_add(args: argparse.Namespace) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from aoi.adapters.protocol import CrawlTargetSpec
    from aoi.db.schema.core import Source
    from aoi.db.schema.enums import TargetRole
    from aoi.db.session import get_engine
    from aoi.tasks.targets import add_target

    with Session(get_engine()) as session, session.begin():
        source = session.scalars(select(Source).where(Source.name == args.source)).first()
        if source is None:
            sys.exit(f"no source named {args.source!r}")
        spec = CrawlTargetSpec(url=args.url, role=TargetRole(args.role))
        target = add_target(session, source.id, spec)
        log.info("target.added", source=source.name, url=target.url, role=target.role)


def _target_retire(args: argparse.Namespace) -> None:
    _set_retired(args, retire=True)


def _target_restore(args: argparse.Namespace) -> None:
    _set_retired(args, retire=False)


def _set_retired(args: argparse.Namespace, *, retire: bool) -> None:
    """Retire the source's active targets whose URL contains the match, or restore its retired
    ones; each URL changed is logged, since a substring can match more than intended."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from aoi.db.schema.core import Source
    from aoi.db.schema.ops import CrawlTarget
    from aoi.db.session import get_engine

    event = "target.retired" if retire else "target.restored"
    with Session(get_engine()) as session, session.begin():
        source = session.scalars(select(Source).where(Source.name == args.source)).first()
        if source is None:
            sys.exit(f"no source named {args.source!r}")
        targets = session.scalars(
            select(CrawlTarget)
            .where(
                CrawlTarget.source_id == source.id,
                CrawlTarget.retired_at.is_(None)
                if retire
                else CrawlTarget.retired_at.is_not(None),
                CrawlTarget.url.contains(args.match, autoescape=True),
            )
            .order_by(CrawlTarget.url)
        ).all()
        for target in targets:
            log.info(event, url=target.url)
            target.retired_at = func.now() if retire else None
        log.info(event, source=source.name, match=args.match, targets=len(targets))


def _task_requeue(args: argparse.Namespace) -> None:
    from sqlalchemy.orm import Session

    from aoi.db.session import get_engine
    from aoi.tasks.queue import NotRequeueable, requeue

    try:
        with Session(get_engine()) as session, session.begin():
            requeue(session, args.task_id)
    except NotRequeueable as exc:
        sys.exit(f"not requeued: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    from aoi.db.schema.enums import TargetRole

    parser = argparse.ArgumentParser(prog="aoi", description="AOI CLI")
    sub = parser.add_subparsers(dest="command")

    # ror -------------------------------------------------------------------
    ror = sub.add_parser("ror", help="ROR organization registry (ADR 0009)")
    ror_sub = ror.add_subparsers(dest="ror_command")

    ror_sync = ror_sub.add_parser("sync", help="Load a ROR data dump zip from Zenodo")
    ror_sync.add_argument("--file", required=True, help="Path to <release>-ror-data.zip")
    ror_sync.set_defaults(func=_ror_sync)

    # taxonomy --------------------------------------------------------------
    from aoi.reference.taxonomy import TAXONOMY_FILE

    taxonomy = sub.add_parser("taxonomy", help="Discipline taxonomy")
    taxonomy_sub = taxonomy.add_subparsers(dest="taxonomy_command")

    tax_sync = taxonomy_sub.add_parser("sync", help="Load ontology/disciplines.yaml")
    tax_sync.add_argument("--file", default=str(TAXONOMY_FILE), help="Taxonomy YAML path")
    tax_sync.set_defaults(func=_taxonomy_sync)

    # worker and scheduler (plan §4.3) --------------------------------------
    worker = sub.add_parser("worker", help="Claim and run tasks until SIGTERM")
    worker.add_argument(
        "--kinds", required=True, help="Comma-separated task kinds, e.g. fetch,extract"
    )
    worker.add_argument(
        "--idle", type=float, default=1.0, help="Seconds to wait when no task is due"
    )
    worker.set_defaults(func=_worker)

    scheduler = sub.add_parser("scheduler", help="Queue due work every interval until SIGTERM")
    scheduler.add_argument("--every", type=float, default=60.0, help="Seconds between ticks")
    scheduler.add_argument(
        "--lease", type=float, default=600.0, help="Seconds before a running task is reaped"
    )
    scheduler.add_argument("--once", action="store_true", help="Run one tick and exit")
    scheduler.set_defaults(func=_scheduler)

    replay = sub.add_parser(
        "replay", help="Re-extract a source's stored evidence with the current adapter (ADR 0006)"
    )
    replay.add_argument("--source", required=True, help="Source name")
    replay.set_defaults(func=_replay)

    lifecycle = sub.add_parser(
        "lifecycle", help="Evaluate a source's posting lifecycle now (ADR 0033)"
    )
    lifecycle.add_argument("--source", required=True, help="Source name")
    lifecycle.add_argument(
        "--allow-mass-closure",
        action="store_true",
        help="Apply a mass closure the breaker would hold (after review)",
    )
    lifecycle.set_defaults(func=_lifecycle)

    target = sub.add_parser("target", help="Crawl target commands")
    target_sub = target.add_subparsers(dest="target_command")
    target_add = target_sub.add_parser(
        "add", help="Add a crawl target, due at once (e.g. a one-off archive sweep)"
    )
    target_add.add_argument("--source", required=True, help="Source name")
    target_add.add_argument(
        "--role", required=True, choices=[r.value for r in TargetRole], help="Target role"
    )
    target_add.add_argument("url", help="URL to crawl")
    target_add.set_defaults(func=_target_add)
    target_retire = target_sub.add_parser(
        "retire",
        help="Retire a source's targets whose URL contains a string (kept, never fetched)",
    )
    target_retire.add_argument("--source", required=True, help="Source name")
    target_retire.add_argument("--match", required=True, help="Substring of the URLs to retire")
    target_retire.set_defaults(func=_target_retire)
    target_restore = target_sub.add_parser(
        "restore", help="Restore a source's retired targets whose URL contains a string"
    )
    target_restore.add_argument("--source", required=True, help="Source name")
    target_restore.add_argument("--match", required=True, help="Substring of the URLs to restore")
    target_restore.set_defaults(func=_target_restore)

    task = sub.add_parser("task", help="Task queue commands")
    task_sub = task.add_subparsers(dest="task_command")
    requeue = task_sub.add_parser(
        "requeue", help="Queue a fresh copy of a dead task after review (plan §4.3)"
    )
    requeue.add_argument("task_id", type=uuid.UUID, help="Id of the dead task")
    requeue.set_defaults(func=_task_requeue)

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
