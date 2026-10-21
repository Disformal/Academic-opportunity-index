"""Tests for the AOI alerting script (scripts/notify.py)."""

import sys
from pathlib import Path

# Add project root so scripts/notify.py can import aoi
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.notify import check_run_result


class TestCheckRunResult:
    def test_no_alerts_on_healthy_run(self) -> None:
        alerts = check_run_result(
            source="INSPIRE-HEP Jobs",
            created=50,
            updated=10,
            errors=0,
            closed=5,
            quarantined=0,
        )
        assert alerts == []

    def test_alert_on_circuit_breaker_fired(self) -> None:
        alerts = check_run_result(
            source="EURAXESS",
            created=10,
            updated=5,
            errors=0,
            closed=0,
            quarantined=20,
        )
        assert len(alerts) == 1
        assert "Circuit breaker" in alerts[0]
        assert "EURAXESS" in alerts[0]
        assert "20" in alerts[0]

    def test_alert_on_zero_new_records(self) -> None:
        alerts = check_run_result(
            source="INSPIRE-HEP Jobs",
            created=0,
            updated=0,
            errors=0,
            closed=3,
            quarantined=0,
        )
        assert len(alerts) == 1
        assert "Zero new" in alerts[0]

    def test_no_alert_when_updates_exist(self) -> None:
        """Updated records are not zero — should not alert."""
        alerts = check_run_result(
            source="EURAXESS",
            created=0,
            updated=5,
            errors=0,
            closed=0,
            quarantined=0,
        )
        assert alerts == []

    def test_alert_on_high_error_rate(self) -> None:
        alerts = check_run_result(
            source="EURAXESS",
            created=5,
            updated=3,
            errors=10,
            closed=0,
            quarantined=0,
        )
        assert len(alerts) == 1
        assert "error rate" in alerts[0].lower()

    def test_no_error_alert_below_threshold(self) -> None:
        """1 error out of 100 is well below 5%."""
        alerts = check_run_result(
            source="INSPIRE-HEP Jobs",
            created=99,
            updated=0,
            errors=1,
            closed=0,
            quarantined=0,
        )
        assert alerts == []

    def test_multiple_alerts_can_fire(self) -> None:
        """Circuit breaker + zero records should produce two alerts."""
        alerts = check_run_result(
            source="EURAXESS",
            created=0,
            updated=0,
            errors=0,
            closed=0,
            quarantined=15,
        )
        assert len(alerts) == 2
        subjects = [a.lower() for a in alerts]
        assert any("circuit breaker" in s for s in subjects)
        assert any("zero" in s for s in subjects)
