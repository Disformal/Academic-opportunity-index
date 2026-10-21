"""Value canonicalization — choosing which source's value wins.

When two sources assert different values for the same field, the system
must pick a winner and explain why.  The score combines derivation quality,
first-party status, trust tier, recency decay, and extraction confidence.

Weights live in ``ontology/canonicalization_weights.yaml`` so they can be
reviewed and tuned as data, not buried in code.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ONTOLOGY = Path(__file__).resolve().parents[2] / "ontology"


@lru_cache(maxsize=1)
def _weights() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((ONTOLOGY / "canonicalization_weights.yaml").read_text())
    return data


@dataclass(frozen=True)
class Candidate:
    value: object
    derivation: str
    first_party: bool
    trust_tier: int
    confidence: float
    created_at: datetime
    source_name: str


def score(c: Candidate, now: datetime | None = None) -> float:
    """Score a single field-value candidate. Higher is better."""
    w = _weights()
    now = now or datetime.now(UTC)
    age = max((now - c.created_at).days, 0)
    decay = math.exp(-age / w["recency_half_life_days"])
    result: float = (
        w["derivation"][c.derivation]
        * w["first_party"][c.first_party]
        * w["trust_tier"][c.trust_tier]
        * decay
        * c.confidence
    )
    return result


def choose(
    field: str,
    cands: list[Candidate],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pick a winner, and say whether the field should be treated as disputed."""
    ranked = sorted(((score(c, now), c) for c in cands), key=lambda p: -p[0])
    top_score, top = ranked[0]
    w = _weights()

    disputed = False
    if len(ranked) > 1:
        second_score, second = ranked[1]
        threshold = top_score * (1 - w["disputed_if_within_percent"] / 100)
        close = second_score >= threshold
        if close and top.value != second.value and top.first_party and second.first_party:
            disputed = True

    below_bar = field in w["critical_fields"] and top_score < w["critical_minimum_score"]

    return {
        "value": top.value,
        "score": round(top_score, 4),
        "winner": top.source_name,
        "disputed": disputed,
        "publication_state": (
            "disputed" if disputed else "provisional" if below_bar else "verified"
        ),
        "competing": [
            {"value": c.value, "source": c.source_name, "score": round(s, 4)}
            for s, c in ranked[1:4]
        ],
    }
