"""Provenance explanation for every published field of one record.

For each field, report:
  - the value shown
  - which source and derivation won
  - the score it won with
  - what the competing values were
  - the exact character range or JSON pointer in the stored evidence

Everything comes from field_assertion rows — nothing is recomputed.
"""

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from aoi.db.models import FieldAssertion, Source, SourceDocument
from aoi.pipeline.value_scorer import Candidate, choose


def explain(session: Session, opportunity_id: uuid.UUID) -> dict[str, Any]:
    """Build a provenance report for every current field of an opportunity.

    Returns a dict keyed by field_path. Each value contains the winning value,
    score, source, derivation, grounding info, and competing alternatives.
    """
    # Fetch all current assertions with their source info
    rows = (
        session.query(FieldAssertion, Source)
        .join(SourceDocument, FieldAssertion.source_document_id == SourceDocument.id)
        .join(Source, SourceDocument.source_id == Source.id)
        .filter(
            FieldAssertion.opportunity_id == opportunity_id,
            FieldAssertion.is_current.is_(True),
        )
        .all()
    )

    if not rows:
        return {}

    # Group assertions by field_path
    by_field: dict[str, list[tuple[FieldAssertion, Source]]] = defaultdict(list)
    for fa, source in rows:
        by_field[fa.field_path].append((fa, source))

    result: dict[str, Any] = {}

    for field_path, assertions in by_field.items():
        candidates = []
        grounding_map: dict[str, tuple[str | None, int | None, int | None]] = {}

        for fa, source in assertions:
            cand = Candidate(
                value=fa.normalized_value,
                derivation=fa.derivation.value,
                first_party=source.first_party,
                trust_tier=source.trust_tier,
                confidence=float(fa.confidence),
                created_at=fa.created_at,
                source_name=source.name,
            )
            candidates.append(cand)
            grounding_map[source.name] = (
                fa.json_pointer,
                fa.text_start,
                fa.text_end,
            )

        chosen = choose(field_path, candidates)

        # Add grounding info from the winning assertion
        winner_grounding = grounding_map.get(chosen["winner"], (None, None, None))
        chosen["derivation"] = next(
            fa.derivation.value for fa, source in assertions if source.name == chosen["winner"]
        )
        chosen["json_pointer"] = winner_grounding[0]
        chosen["text_start"] = winner_grounding[1]
        chosen["text_end"] = winner_grounding[2]

        result[field_path] = chosen

    return result
