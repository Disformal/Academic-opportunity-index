"""The adapters the pipeline runs, keyed by source platform or, for a bespoke source, its name."""

from aoi.adapters.inspire import InspireAdapter
from aoi.adapters.protocol import Adapter

ADAPTERS: dict[str, Adapter] = {"inspire": InspireAdapter()}
