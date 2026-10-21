"""Mass-closure circuit breaker for AOI.

Prevents catastrophic closures when a source changes its page layout
(parser finds zero items → every record looks delisted in one run).
The breaker turns that into a held batch and a log warning, which costs
a few minutes instead of the index's credibility.

Thresholds are loaded from ``ontology/closure_policy.yaml``.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache(maxsize=1)
def _policy() -> dict[str, int | float]:
    path = Path(__file__).resolve().parent.parent.parent / "ontology" / "closure_policy.yaml"
    with path.open() as f:
        data: dict[str, dict[str, int | float]] = yaml.safe_load(f)
        return data["circuit_breaker"]


def _proportion() -> float:
    return float(_policy()["proportion"])


def _floor() -> int:
    return int(_policy()["floor"])


@dataclass(frozen=True)
class BreakerResult:
    allowed: bool
    message: str


def check(*, active_before: int, would_close: int, source_name: str) -> BreakerResult:
    """Refuse to commit a closure batch that is implausibly large."""
    limit = max(int(active_before * _proportion()), _floor())
    if would_close > limit:
        return BreakerResult(
            False,
            f"QUARANTINED: run would close {would_close} of {active_before} "
            f"records for {source_name} (limit {limit}). "
            f"Nothing was closed. Inspect before releasing.",
        )
    return BreakerResult(True, f"{would_close} closures within limit {limit}")
