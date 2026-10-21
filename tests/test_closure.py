"""Tests for closure detection — aoi.pipeline.closure."""

from datetime import UTC, datetime, timedelta

import pytest

from aoi.db.enums import ClosureReason
from aoi.pipeline.closure import ABSENCE_THRESHOLD, ClosureDecision, decide

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

# Base kwargs for a "normal, still-open" call — override per test.
_OPEN: dict = {
    "http_status": 200,
    "confirmed_gone_twice": False,
    "consecutive_absences": 0,
    "body_text": None,
    "language": "en",
    "deadline": None,
    "now": _NOW,
    "looks_like_soft_404": False,
}


# ---------------------------------------------------------------------------
# Still-open baseline
# ---------------------------------------------------------------------------


class TestStillOpen:
    def test_all_normal_signals_returns_open(self) -> None:
        d = decide(**_OPEN)
        assert d.closed is False
        assert d.reason is None
        assert d.confidence == 0.0

    def test_deadline_in_future_stays_open(self) -> None:
        d = decide(**{**_OPEN, "deadline": _NOW + timedelta(days=7)})
        assert d.closed is False

    def test_deadline_within_24h_stays_open(self) -> None:
        """Deadline less than 24h ago should NOT trigger closure."""
        d = decide(**{**_OPEN, "deadline": _NOW - timedelta(hours=12)})
        assert d.closed is False


# ---------------------------------------------------------------------------
# Soft 404 — highest priority, blocks closure
# ---------------------------------------------------------------------------


class TestSoft404:
    def test_soft_404_blocks_closure(self) -> None:
        """Even with a 404 status, soft-404 suspicion blocks closure."""
        d = decide(
            **{
                **_OPEN,
                "http_status": 404,
                "confirmed_gone_twice": True,
                "looks_like_soft_404": True,
            }
        )
        assert d.closed is False
        assert "soft 404" in d.note.lower()


# ---------------------------------------------------------------------------
# REMOVED_404 — confirmed 404/410 on two fetches
# ---------------------------------------------------------------------------


class TestRemoved404:
    def test_404_confirmed_twice(self) -> None:
        d = decide(**{**_OPEN, "http_status": 404, "confirmed_gone_twice": True})
        assert d.closed is True
        assert d.reason == ClosureReason.REMOVED_404
        assert d.confidence >= 0.95

    def test_410_confirmed_twice(self) -> None:
        d = decide(**{**_OPEN, "http_status": 410, "confirmed_gone_twice": True})
        assert d.closed is True
        assert d.reason == ClosureReason.REMOVED_404

    def test_404_not_confirmed_stays_open(self) -> None:
        """A single 404 (not confirmed) should not close."""
        d = decide(**{**_OPEN, "http_status": 404, "confirmed_gone_twice": False})
        assert d.closed is False

    def test_500_error_stays_open(self) -> None:
        """Server errors are not closure signals."""
        d = decide(**{**_OPEN, "http_status": 500, "confirmed_gone_twice": True})
        assert d.closed is False


# ---------------------------------------------------------------------------
# STATED_CLOSED — phrase matching in body text
# ---------------------------------------------------------------------------


class TestStatedClosed:
    def test_english_phrase_match(self) -> None:
        d = decide(**{**_OPEN, "body_text": "This position filled last week."})
        assert d.closed is True
        assert d.reason == ClosureReason.STATED_CLOSED
        assert d.confidence >= 0.90

    def test_case_insensitive_match(self) -> None:
        d = decide(**{**_OPEN, "body_text": "APPLICATIONS ARE CLOSED effective today."})
        assert d.closed is True
        assert d.reason == ClosureReason.STATED_CLOSED

    def test_german_phrase_match(self) -> None:
        d = decide(**{**_OPEN, "body_text": "Die Bewerbungsfrist abgelaufen.", "language": "de"})
        assert d.closed is True
        assert d.reason == ClosureReason.STATED_CLOSED

    def test_french_phrase_match(self) -> None:
        d = decide(**{**_OPEN, "body_text": "Le poste est pourvu depuis mars.", "language": "fr"})
        assert d.closed is True
        assert d.reason == ClosureReason.STATED_CLOSED

    def test_english_fallback_for_unknown_language(self) -> None:
        """Unknown language should still check English phrases."""
        d = decide(**{**_OPEN, "body_text": "This vacancy has expired.", "language": "xx"})
        assert d.closed is True
        assert d.reason == ClosureReason.STATED_CLOSED

    def test_no_match_stays_open(self) -> None:
        d = decide(**{**_OPEN, "body_text": "We are looking for excellent candidates."})
        assert d.closed is False


# ---------------------------------------------------------------------------
# DELISTED — absent from listing N consecutive times
# ---------------------------------------------------------------------------


class TestDelisted:
    def test_absent_at_threshold(self) -> None:
        d = decide(**{**_OPEN, "consecutive_absences": ABSENCE_THRESHOLD})
        assert d.closed is True
        assert d.reason == ClosureReason.DELISTED
        assert d.confidence >= 0.75

    def test_absent_above_threshold(self) -> None:
        d = decide(**{**_OPEN, "consecutive_absences": ABSENCE_THRESHOLD + 5})
        assert d.closed is True
        assert d.reason == ClosureReason.DELISTED

    def test_absent_below_threshold(self) -> None:
        d = decide(**{**_OPEN, "consecutive_absences": ABSENCE_THRESHOLD - 1})
        assert d.closed is False


# ---------------------------------------------------------------------------
# DEADLINE_PASSED — deadline more than 24h ago
# ---------------------------------------------------------------------------


class TestDeadlinePassed:
    def test_deadline_over_24h_ago(self) -> None:
        d = decide(**{**_OPEN, "deadline": _NOW - timedelta(hours=25)})
        assert d.closed is True
        assert d.reason == ClosureReason.DEADLINE_PASSED
        assert d.confidence >= 0.90

    def test_deadline_exactly_24h_ago_stays_open(self) -> None:
        """Boundary: deadline exactly 24h ago should stay open (strict less-than)."""
        d = decide(**{**_OPEN, "deadline": _NOW - timedelta(hours=24)})
        assert d.closed is False

    def test_deadline_far_past(self) -> None:
        d = decide(**{**_OPEN, "deadline": _NOW - timedelta(days=90)})
        assert d.closed is True
        assert d.reason == ClosureReason.DEADLINE_PASSED


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestPriority:
    def test_soft_404_beats_confirmed_404(self) -> None:
        d = decide(
            **{
                **_OPEN,
                "http_status": 404,
                "confirmed_gone_twice": True,
                "looks_like_soft_404": True,
            }
        )
        assert d.closed is False

    def test_404_beats_stated_closed(self) -> None:
        """404 confirmed should take precedence over body-text phrase match."""
        d = decide(
            **{
                **_OPEN,
                "http_status": 404,
                "confirmed_gone_twice": True,
                "body_text": "position filled",
            }
        )
        assert d.reason == ClosureReason.REMOVED_404

    def test_stated_closed_beats_delisted(self) -> None:
        d = decide(
            **{
                **_OPEN,
                "body_text": "Applications are closed.",
                "consecutive_absences": ABSENCE_THRESHOLD + 1,
            }
        )
        assert d.reason == ClosureReason.STATED_CLOSED

    def test_delisted_beats_deadline_passed(self) -> None:
        d = decide(
            **{
                **_OPEN,
                "consecutive_absences": ABSENCE_THRESHOLD,
                "deadline": _NOW - timedelta(days=7),
            }
        )
        assert d.reason == ClosureReason.DELISTED


# ---------------------------------------------------------------------------
# ClosureDecision dataclass
# ---------------------------------------------------------------------------


class TestClosureDecision:
    def test_frozen(self) -> None:
        d = ClosureDecision(closed=True, reason=ClosureReason.DELISTED, confidence=0.8, note="x")
        with pytest.raises(AttributeError):
            d.closed = False  # type: ignore[misc]
