"""Closure detection for AOI opportunities.

Pure decision function: takes signals about an opportunity's current state
and returns a ClosureDecision.  No database access — the caller gathers
the signals and acts on the result.

Priority order (first match wins):
1. Soft-404 suspicion → do NOT close (raise health alert instead)
2. Confirmed 404/410 on two fetches ≥24 h apart → REMOVED_404
3. Body text contains a closed-phrase (multi-language) → STATED_CLOSED
4. Absent from listing ≥ ABSENCE_THRESHOLD consecutive runs → DELISTED
5. Deadline passed by >24 h with no contradicting signal → DEADLINE_PASSED
6. Otherwise → still open
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aoi.db.enums import ClosureReason

CLOSED_PHRASES: dict[str, list[str]] = {
    "en": [
        "applications are closed",
        "position filled",
        "no longer accepting",
        "this vacancy has expired",
        "closed for applications",
    ],
    "sv": ["ansökningstiden har gått ut"],
    "de": ["bewerbungsfrist abgelaufen", "stelle besetzt"],
    "nl": ["sollicitatietermijn verstreken"],
    "fr": ["le poste est pourvu", "candidatures closes", "plus de candidatures"],
    "it": ["posizione chiusa", "candidature chiuse", "posto ricoperto"],
}

ABSENCE_THRESHOLD: int = 3


@dataclass(frozen=True)
class ClosureDecision:
    closed: bool
    reason: ClosureReason | None
    confidence: float
    note: str


def decide(
    *,
    http_status: int | None,
    confirmed_gone_twice: bool,
    consecutive_absences: int,
    body_text: str | None,
    language: str,
    deadline: datetime | None,
    now: datetime | None = None,
    looks_like_soft_404: bool = False,
) -> ClosureDecision:
    """Evaluate whether an opportunity should be marked as closed.

    All parameters are keyword-only to prevent positional mistakes.
    """
    now = now or datetime.now(UTC)

    # 1. Soft-404 suspicion blocks everything
    if looks_like_soft_404:
        return ClosureDecision(
            False, None, 0.0, "soft 404 suspected - raise health alert, do not close"
        )

    # 2. Confirmed 404/410
    if http_status in (404, 410) and confirmed_gone_twice:
        return ClosureDecision(
            True, ClosureReason.REMOVED_404, 0.97, "gone on two fetches at least 24h apart"
        )

    # 3. Body-text phrase match
    if body_text:
        low = body_text.lower()
        for phrase in CLOSED_PHRASES.get(language, []) + CLOSED_PHRASES["en"]:
            if phrase in low:
                return ClosureDecision(
                    True, ClosureReason.STATED_CLOSED, 0.95, f"matched phrase: {phrase!r}"
                )

    # 4. Delisted (absent from listing N times)
    if consecutive_absences >= ABSENCE_THRESHOLD:
        return ClosureDecision(
            True,
            ClosureReason.DELISTED,
            0.80,
            f"absent from listing {consecutive_absences} times",
        )

    # 5. Deadline passed by >24 h
    if deadline and deadline < now - timedelta(hours=24):
        return ClosureDecision(
            True,
            ClosureReason.DEADLINE_PASSED,
            0.93,
            "deadline passed with no contradicting signal",
        )

    # 6. Still open
    return ClosureDecision(False, None, 0.0, "still open")
