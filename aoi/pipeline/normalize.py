"""Date normalization for AOI — timezone ladder that never invents a time."""

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog
from dateutil import parser as dateparser
from dateutil.parser import ParserError

log = structlog.get_logger()


@dataclass(frozen=True)
class NormalizedDate:
    value: datetime  # always UTC
    precision: str  # 'instant' | 'day'
    rule: str  # which rung fired
    confidence: float
    original: str


def normalize_deadline(
    raw: str,
    *,
    page_timezone: ZoneInfo | None = None,
    source_timezone: ZoneInfo | None = None,
    org_timezone: ZoneInfo | None = None,
) -> NormalizedDate | None:
    """Normalize a raw deadline string to a UTC datetime with provenance.

    Timezone ladder (highest confidence wins):
      Rung 1: explicit offset in string → conf 0.99, precision 'instant'
      Rung 2: page_timezone provided    → conf 0.95, precision 'instant'
      Rung 3: source_timezone provided  → conf 0.90, precision 'instant'
      Rung 4: org_timezone provided     → conf 0.75, precision 'instant'
      Rung 5: no timezone available     → conf 0.60, precision 'day'

    Never invents a time when timezone is unknown.
    Returns None for empty, None, or unparseable input.
    """
    if not raw:
        return None

    try:
        parsed = dateparser.parse(raw, ignoretz=False)
    except (ParserError, ValueError, OverflowError):
        log.debug("normalize.parse_failed", raw=raw)
        return None

    if parsed is None:
        return None

    # Rung 1: string carries explicit offset
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        utc = parsed.astimezone(UTC).replace(tzinfo=UTC)
        return NormalizedDate(
            value=utc,
            precision="instant",
            rule="explicit_offset",
            confidence=0.99,
            original=raw,
        )

    # Rungs 2-4: apply context timezone (parsed has no tzinfo)
    tz_ladder: list[tuple[ZoneInfo | None, float, str]] = [
        (page_timezone, 0.95, "page_timezone"),
        (source_timezone, 0.90, "source_timezone"),
        (org_timezone, 0.75, "org_timezone"),
    ]
    for tz, conf, rule in tz_ladder:
        if tz is not None:
            localized = parsed.replace(tzinfo=tz)
            utc = localized.astimezone(UTC).replace(tzinfo=UTC)
            return NormalizedDate(
                value=utc,
                precision="instant",
                rule=rule,
                confidence=conf,
                original=raw,
            )

    # Rung 5: no timezone — store date only, no time invented
    naive_midnight = datetime(parsed.year, parsed.month, parsed.day, 0, 0, 0, tzinfo=UTC)
    return NormalizedDate(
        value=naive_midnight,
        precision="day",
        rule="date_only",
        confidence=0.60,
        original=raw,
    )
