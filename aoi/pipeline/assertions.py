"""Provenance assertion writer for AOI."""

import uuid
from typing import Any

import structlog
from sqlalchemy.orm import Session

from aoi.db.enums import Derivation
from aoi.db.models import FieldAssertion

log = structlog.get_logger()

# Derivations that require no grounding (no json_pointer and no text_start needed)
_UNGROUNDED_EXEMPT = {Derivation.HUMAN, Derivation.INHERITED}


class UngroundedAssertion(Exception):
    """Raised when an assertion lacks the required grounding."""


def assert_field(
    session: Session,
    *,
    opportunity_id: uuid.UUID,
    field_path: str,
    value: Any,
    raw_value: str | None,
    confidence: float,
    derivation: Derivation,
    source_document_id: uuid.UUID,
    extraction_run_id: uuid.UUID,
    text_start: int | None = None,
    text_end: int | None = None,
    json_pointer: str | None = None,
    source_text: str | None = None,
) -> FieldAssertion:
    """Write a provenance-grounded FieldAssertion, superseding any previous current assertion.

    Grounding rules:
    - Every assertion must be grounded (json_pointer OR text_start) unless HUMAN or INHERITED.
    - If text_start, text_end, source_text, and raw_value are all provided: verify that
      raw_value.strip() appears within source_text[text_start:text_end].

    Supersession:
    - All existing is_current=True assertions for this (opportunity_id, field_path) are set
      to is_current=False before creating the new one.
    """
    # Grounding check
    if derivation not in _UNGROUNDED_EXEMPT and json_pointer is None and text_start is None:
        raise UngroundedAssertion(
            f"Assertion for field {field_path!r} with derivation {derivation!r} "
            "requires json_pointer or text_start"
        )

    # Offset verification
    if (
        text_start is not None
        and text_end is not None
        and source_text is not None
        and raw_value is not None
    ):
        window = source_text[text_start:text_end]
        if raw_value.strip() not in window:
            raise UngroundedAssertion(
                f"raw_value {raw_value!r} not found in source_text window "
                f"[{text_start}:{text_end}] = {window!r}"
            )

    # Supersede previous current assertions for this field
    previous = (
        session.query(FieldAssertion)
        .filter_by(opportunity_id=opportunity_id, field_path=field_path, is_current=True)
        .all()
    )
    for prev in previous:
        prev.is_current = False
    if previous:
        session.flush()
        log.debug(
            "assertions.superseded",
            opportunity_id=str(opportunity_id),
            field_path=field_path,
            count=len(previous),
        )

    fa = FieldAssertion(
        opportunity_id=opportunity_id,
        field_path=field_path,
        normalized_value=value,
        raw_value=raw_value,
        confidence=confidence,
        derivation=derivation,
        source_document_id=source_document_id,
        extraction_run_id=extraction_run_id,
        text_start=text_start,
        text_end=text_end,
        json_pointer=json_pointer,
        is_current=True,
    )
    session.add(fa)
    session.flush()

    log.debug(
        "assertions.created",
        opportunity_id=str(opportunity_id),
        field_path=field_path,
        derivation=derivation.value,
        confidence=confidence,
    )

    return fa
