"""Tests for the mass-closure circuit breaker — aoi.pipeline.circuit_breaker."""

from aoi.pipeline.circuit_breaker import check


class TestCircuitBreaker:
    def test_normal_attrition_allowed(self) -> None:
        """Closing a small fraction of records should be allowed."""
        result = check(active_before=500, would_close=40, source_name="inspire")
        assert result.allowed is True

    def test_mass_closure_blocked(self) -> None:
        """Closing most records should be quarantined."""
        result = check(active_before=500, would_close=400, source_name="inspire")
        assert result.allowed is False
        assert "QUARANTINED" in result.message

    def test_small_source_uses_floor(self) -> None:
        """For small sources, the floor (20) overrides the proportion limit."""
        # 15% of 30 = 4, but the floor of 20 applies → 15 closures OK
        result = check(active_before=30, would_close=15, source_name="euraxess")
        assert result.allowed is True

    def test_exactly_at_limit_allowed(self) -> None:
        """Closing exactly the limit should be allowed (not blocked)."""
        # 15% of 500 = 75
        result = check(active_before=500, would_close=75, source_name="inspire")
        assert result.allowed is True

    def test_one_over_limit_blocked(self) -> None:
        """One closure over the limit should be blocked."""
        # 15% of 500 = 75; 76 should be blocked
        result = check(active_before=500, would_close=76, source_name="inspire")
        assert result.allowed is False

    def test_zero_closures_always_allowed(self) -> None:
        result = check(active_before=500, would_close=0, source_name="inspire")
        assert result.allowed is True

    def test_zero_active_uses_floor(self) -> None:
        """When there are no active records, the floor still applies."""
        result = check(active_before=0, would_close=0, source_name="inspire")
        assert result.allowed is True

    def test_message_includes_source_name(self) -> None:
        result = check(active_before=500, would_close=400, source_name="euraxess")
        assert "euraxess" in result.message

    def test_message_includes_counts(self) -> None:
        result = check(active_before=500, would_close=400, source_name="inspire")
        assert "400" in result.message
        assert "500" in result.message
