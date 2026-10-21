"""Task handlers: one per task kind (plan §4.3). Handlers never commit; the worker does."""

from collections.abc import Mapping, Sequence

from aoi.adapters.protocol import Adapter
from aoi.tasks.handlers.discover import make_discover_handler
from aoi.tasks.handlers.extract import make_extract_handler
from aoi.tasks.handlers.fetch import handle_fetch
from aoi.tasks.handlers.lifecycle import handle_lifecycle
from aoi.tasks.handlers.resolve import handle_resolve
from aoi.tasks.worker import Handler


def handlers_for(kinds: Sequence[str], adapters: Mapping[str, Adapter]) -> dict[str, Handler]:
    """The handlers for `kinds`; an unknown kind is refused rather than left unclaimed."""
    available: dict[str, Handler] = {
        "fetch": handle_fetch,
        "extract": make_extract_handler(adapters),
        "discover": make_discover_handler(adapters),
        "resolve": handle_resolve,
        "lifecycle": handle_lifecycle,
    }
    unknown = sorted(set(kinds) - available.keys())
    if unknown:
        raise ValueError(f"unknown task kinds: {unknown}; known: {sorted(available)}")
    return {kind: available[kind] for kind in kinds}
