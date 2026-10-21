"""Tests for date normalization (normalize.py)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from aoi.pipeline.normalize import normalize_deadline


def test_normalize_deadline_explicit_offset_produces_instant_conf_99() -> None:
    result = normalize_deadline("2024-12-31T23:59:00+02:00")
    assert result is not None
    assert result.precision == "instant"
    assert abs(result.confidence - 0.99) < 1e-9
    assert result.value.tzinfo is not None
    # 2024-12-31T23:59:00+02:00 → 2024-12-31T21:59:00 UTC
    assert result.value == datetime(2024, 12, 31, 21, 59, 0, tzinfo=UTC)


def test_normalize_deadline_explicit_utc_offset_zero() -> None:
    result = normalize_deadline("2025-06-15T12:00:00+00:00")
    assert result is not None
    assert result.precision == "instant"
    assert result.confidence == pytest.approx(0.99)
    assert result.value == datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)


def test_normalize_deadline_no_timezone_no_time_produces_day_conf_60() -> None:
    result = normalize_deadline("2024-09-30")
    assert result is not None
    assert result.precision == "day"
    assert result.confidence == pytest.approx(0.60)
    assert result.value.date().isoformat() == "2024-09-30"


def test_normalize_deadline_source_timezone_applied_conf_90() -> None:
    result = normalize_deadline(
        "2025-03-15 17:00",
        source_timezone=ZoneInfo("America/New_York"),
    )
    assert result is not None
    assert result.precision == "instant"
    assert result.confidence == pytest.approx(0.90)
    # America/New_York in March is UTC-4 (DST active on March 15 2025 after March 9)
    expected = datetime(2025, 3, 15, 21, 0, 0, tzinfo=UTC)
    assert result.value == expected


def test_normalize_deadline_page_timezone_applied_conf_95() -> None:
    result = normalize_deadline(
        "2025-01-20 10:00",
        page_timezone=ZoneInfo("Europe/Berlin"),
    )
    assert result is not None
    assert result.precision == "instant"
    assert result.confidence == pytest.approx(0.95)
    # Europe/Berlin in January is UTC+1
    expected = datetime(2025, 1, 20, 9, 0, 0, tzinfo=UTC)
    assert result.value == expected


def test_normalize_deadline_org_timezone_applied_conf_75() -> None:
    result = normalize_deadline(
        "2025-07-04 09:00",
        org_timezone=ZoneInfo("Asia/Tokyo"),
    )
    assert result is not None
    assert result.precision == "instant"
    assert result.confidence == pytest.approx(0.75)
    # Asia/Tokyo is UTC+9
    expected = datetime(2025, 7, 4, 0, 0, 0, tzinfo=UTC)
    assert result.value == expected


def test_normalize_deadline_page_timezone_wins_over_source() -> None:
    # page_timezone (conf 0.95) should be used over source_timezone (conf 0.90)
    result = normalize_deadline(
        "2025-05-10 08:00",
        page_timezone=ZoneInfo("Europe/Paris"),
        source_timezone=ZoneInfo("Asia/Tokyo"),
    )
    assert result is not None
    assert result.confidence == pytest.approx(0.95)


def test_normalize_deadline_none_returns_none() -> None:
    assert normalize_deadline(None) is None  # type: ignore[arg-type]


def test_normalize_deadline_empty_string_returns_none() -> None:
    assert normalize_deadline("") is None


def test_normalize_deadline_invalid_string_returns_none() -> None:
    assert normalize_deadline("not a date at all") is None


def test_normalize_deadline_preserves_original() -> None:
    raw = "2024-12-01T09:00:00+01:00"
    result = normalize_deadline(raw)
    assert result is not None
    assert result.original == raw


def test_normalize_deadline_result_is_always_utc() -> None:
    result = normalize_deadline("2025-08-20T15:30:00+05:30")
    assert result is not None
    assert result.value.tzinfo == UTC


def test_normalize_deadline_date_only_no_time_invented() -> None:
    # When no timezone is available, precision must be 'day' — no time should be invented
    result = normalize_deadline("2025-11-15")
    assert result is not None
    assert result.precision == "day"
    # We should not have H:M:S information that implies a specific time was chosen
    assert result.value.hour == 0
    assert result.value.minute == 0
    assert result.value.second == 0
