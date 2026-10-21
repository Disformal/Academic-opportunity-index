"""The discover handler (plan §4.4): a source's seed URLs from its adapter become crawl targets."""

from collections.abc import Mapping

import structlog
from sqlalchemy.orm import Session

from aoi.adapters.protocol import Adapter
from aoi.db.schema.core import Source
from aoi.db.schema.ops import Task
from aoi.tasks.targets import add_target
from aoi.tasks.worker import Handler

log = structlog.get_logger()


def make_discover_handler(adapters: Mapping[str, Adapter]) -> Handler:
    """A discover handler; a source's adapter is keyed by its platform, or else its name."""

    def handle_discover(session: Session, task: Task) -> None:
        source = session.get_one(Source, task.source_id)
        adapter = adapters[source.platform or source.name]
        seeds = [add_target(session, source.id, spec) for spec in adapter.discover(source)]
        log.info("discover.done", source=source.name, seeds=len(seeds))

    return handle_discover
