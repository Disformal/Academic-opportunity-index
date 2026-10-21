"""Non-destructive opportunity versioning for AOI.

Every time an opportunity's fields change, a new OpportunityVersion is
created with a full snapshot of all current FieldAssertions.  If the
fields are identical to the previous version, no new version is written
(idempotent re-runs).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from aoi.db.models import FieldAssertion, OpportunityVersion

log = structlog.get_logger()


def snapshot(session: Session, opportunity_id: uuid.UUID) -> dict[str, Any]:
    """Return a dict of ``{field_path: normalized_value}`` for all current assertions."""
    rows = (
        session.query(FieldAssertion)
        .filter_by(opportunity_id=opportunity_id, is_current=True)
        .all()
    )
    result: dict[str, Any] = {}
    for row in rows:
        result[row.field_path] = row.normalized_value
    return result


def maybe_version(
    session: Session,
    opportunity_id: uuid.UUID,
    *,
    extraction_run_id: uuid.UUID,
    change_reason: str,
) -> OpportunityVersion | None:
    """Create a new OpportunityVersion if the current state differs from the latest version.

    Returns the new version, or ``None`` if the state is unchanged (idempotent).
    """
    current = snapshot(session, opportunity_id)

    # Find the latest existing version
    latest = (
        session.query(OpportunityVersion)
        .filter_by(opportunity_id=opportunity_id)
        .order_by(OpportunityVersion.version_number.desc())
        .first()
    )

    if latest is not None and latest.payload == current:
        log.debug(
            "versioning.unchanged",
            opportunity_id=str(opportunity_id),
            version_number=latest.version_number,
        )
        return None

    next_number = (latest.version_number + 1) if latest is not None else 1

    # Build change summary
    change_summary: str | None = None
    if latest is not None:
        changed_fields = []
        all_keys = set(latest.payload.keys()) | set(current.keys())
        for key in sorted(all_keys):
            old = latest.payload.get(key)
            new = current.get(key)
            if old != new:
                changed_fields.append(key)
        if changed_fields:
            change_summary = "Changed: " + ", ".join(changed_fields)

    version = OpportunityVersion(
        opportunity_id=opportunity_id,
        version_number=next_number,
        recorded_at=datetime.now(UTC),
        payload=current,
        change_summary=change_summary,
        change_reason=change_reason,
        caused_by_extraction_run_id=extraction_run_id,
    )
    session.add(version)
    session.flush()

    log.info(
        "versioning.created",
        opportunity_id=str(opportunity_id),
        version_number=next_number,
        change_summary=change_summary,
    )

    return version
