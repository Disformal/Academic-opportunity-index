"""Deduplication — candidate finding, scoring, and classification.

Two records from different sources may describe the same position.
This module finds plausible matches (cheap recall), scores them with
interpretable evidence weights, and classifies each pair as
auto_merge, review, or distinct.

Weights are expressed in bits, summed, and squashed through a sigmoid.
When a curator asks why two records merged, the answer is a table of
signals — not a black-box score.
"""

import csv
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import IO, Any

from rapidfuzz import fuzz
from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import Session

from aoi.db.enums import PublicationState
from aoi.db.models import DuplicateCluster, DuplicateMember, Opportunity
from aoi.pipeline.identity import normalize_title

AUTO_MERGE = 0.97
REVIEW = 0.85


@dataclass(frozen=True)
class Scored:
    other_id: str
    probability: float
    signals: dict[str, float]


def candidates(session: Session, opp: Opportunity) -> list[Opportunity]:
    """Recall-oriented and cheap: narrow to a few dozen plausible matches."""
    q = session.query(Opportunity).filter(Opportunity.id != opp.id)
    filters: list[ColumnElement[bool]] = []
    if opp.application_url:
        filters.append(Opportunity.application_url == opp.application_url)
    if opp.deadline:
        filters.append(
            Opportunity.deadline.between(
                opp.deadline - timedelta(days=1),
                opp.deadline + timedelta(days=1),
            )
        )
    if not filters:
        return []
    return q.filter(or_(*filters)).limit(60).all()


def score(a: Opportunity, b: Opportunity) -> Scored:
    """Interpretable evidence weights, in bits, summed and squashed."""
    s: dict[str, float] = {}

    t = fuzz.token_set_ratio(normalize_title(a.title), normalize_title(b.title)) / 100.0
    s["title"] = 3.0 * t - 1.0

    if a.application_url and a.application_url == b.application_url:
        s["application_url"] = 4.0
    if a.country and b.country:
        s["country"] = 0.8 if a.country == b.country else -2.5
    if a.deadline and b.deadline:
        days = abs((a.deadline - b.deadline).days)
        s["deadline"] = 2.5 if days == 0 else (1.0 if days <= 3 else -2.0)

    total = sum(s.values())
    return Scored(str(b.id), 1.0 / (1.0 + math.exp(-total)), s)


def classify(sc: Scored) -> str:
    """Classify a scored pair as auto_merge, review, or distinct."""
    strong = sum(1 for v in sc.signals.values() if v >= 2.0)
    if sc.probability >= AUTO_MERGE and strong >= 2:
        return "auto_merge"
    if sc.probability >= REVIEW:
        return "review"
    return "distinct"


# ---------------------------------------------------------------------------
# Merge / Unmerge
# ---------------------------------------------------------------------------


def merge(
    session: Session,
    a: Opportunity,
    b: Opportunity,
    *,
    score: float,
    method: str = "auto",
    signals: dict[str, float] | None = None,
) -> DuplicateCluster:
    """Record that *a* and *b* are duplicates, with *a* as canonical.

    Both rows survive — merging never deletes a record.
    """
    now = datetime.now(UTC)

    cluster = DuplicateCluster(
        canonical_opportunity_id=a.id,
        method=method,
        score=score,
    )
    session.add(cluster)
    session.flush()

    for opp in (a, b):
        member = DuplicateMember(
            cluster_id=cluster.id,
            opportunity_id=opp.id,
            probability=score,
            signals=signals,
            added_at=now,
        )
        session.add(member)

    session.flush()
    return cluster


def unmerge(session: Session, cluster_id: uuid.UUID) -> None:
    """Dissolve a duplicate cluster, restoring members to VERIFIED state."""
    cluster = session.get(DuplicateCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Cluster {cluster_id} not found")

    member_ids = [m.opportunity_id for m in cluster.members]

    for member in list(cluster.members):
        session.delete(member)
    session.delete(cluster)
    session.flush()

    for oid in member_ids:
        opp = session.get(Opportunity, oid)
        if opp is not None:
            opp.publication_state = PublicationState.VERIFIED
    session.flush()


# ---------------------------------------------------------------------------
# CLI helpers — export / evaluate
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "id_a",
    "id_b",
    "title_a",
    "title_b",
    "probability",
    "classification",
    "signals",
    "label",
]


def export_candidates_csv(session: Session, out: IO[str], *, limit: int = 200) -> int:
    """Write candidate pairs to *out* as CSV. Returns number of rows written."""
    writer = csv.DictWriter(out, fieldnames=_CSV_FIELDS)
    writer.writeheader()

    seen: set[tuple[str, str]] = set()
    count = 0
    opps = session.query(Opportunity).all()

    for opp in opps:
        if count >= limit:
            break
        for other in candidates(session, opp):
            ids = sorted((str(opp.id), str(other.id)))
            pair = (ids[0], ids[1])
            if pair in seen:
                continue
            seen.add(pair)
            sc = score(opp, other)
            writer.writerow(
                {
                    "id_a": str(opp.id),
                    "id_b": str(other.id),
                    "title_a": opp.title,
                    "title_b": other.title,
                    "probability": f"{sc.probability:.4f}",
                    "classification": classify(sc),
                    "signals": json.dumps({k: round(v, 3) for k, v in sc.signals.items()}),
                    "label": "",
                }
            )
            count += 1
            if count >= limit:
                break
    return count


_THRESHOLDS = [0.97, 0.95, 0.90, 0.85, 0.80]


def evaluate_pairs(pairs_file: IO[str]) -> list[dict[str, Any]]:
    """Compute precision/recall at various thresholds from labelled CSV."""
    reader = csv.DictReader(pairs_file)
    rows = [
        (float(r["probability"]), r["label"].strip().lower())
        for r in reader
        if r.get("label", "").strip()
    ]

    results: list[dict[str, Any]] = []
    total_same = sum(1 for _, label in rows if label == "same")

    for threshold in _THRESHOLDS:
        predicted_same = [(p, lb) for p, lb in rows if p >= threshold]
        true_positives = sum(1 for _, lb in predicted_same if lb == "same")
        false_positives = sum(1 for _, lb in predicted_same if lb != "same")

        precision = (
            true_positives / (true_positives + false_positives)
            if (true_positives + false_positives) > 0
            else 0.0
        )
        recall = true_positives / total_same if total_same > 0 else 0.0

        results.append(
            {
                "threshold": threshold,
                "precision": round(precision, 3),
                "recall": round(recall, 3),
                "false_merges": false_positives,
                "total": len(rows),
            }
        )

    return results
