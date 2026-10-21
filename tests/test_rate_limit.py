"""Tests for per-host token bucket rate limiter — Stage 4."""

import time

# ---------------------------------------------------------------------------
# 1. Burst: rapid calls to same host are spaced out
# ---------------------------------------------------------------------------


def test_rate_limiter_burst_calls_are_spaced() -> None:
    """Multiple rapid calls to the same host are spaced at least 1/per_second apart."""
    from aoi.pipeline.rate_limit import RateLimiter

    limiter = RateLimiter()
    per_second = 5.0  # 200ms gap
    host = "example.com"

    timestamps: list[float] = []
    for _ in range(3):
        limiter.wait(host, per_second)
        timestamps.append(time.monotonic())

    # After the first call, each subsequent call must wait ~200ms
    gap = 1.0 / per_second
    # Allow 20ms slack for system jitter
    for i in range(1, len(timestamps)):
        elapsed = timestamps[i] - timestamps[i - 1]
        assert elapsed >= gap - 0.02, (
            f"Gap between call {i - 1} and {i} was {elapsed:.3f}s, expected >= {gap - 0.02:.3f}s"
        )


# ---------------------------------------------------------------------------
# 2. Per-host isolation: two hosts don't interfere
# ---------------------------------------------------------------------------


def test_rate_limiter_per_host_isolation() -> None:
    """Calls to two different hosts are not delayed by each other."""
    from aoi.pipeline.rate_limit import RateLimiter

    limiter = RateLimiter()
    per_second = 2.0  # 500ms gap

    # Call host A first to prime it
    limiter.wait("host-a.com", per_second)

    # Call host B immediately — should not be delayed by host A's state
    start = time.monotonic()
    limiter.wait("host-b.com", per_second)
    elapsed = time.monotonic() - start

    # host B is fresh — should not wait for host A's gap
    assert elapsed < 0.1, f"host-b.com waited {elapsed:.3f}s despite being fresh"


# ---------------------------------------------------------------------------
# 3. Steady state: calls spaced >= gap apart don't sleep
# ---------------------------------------------------------------------------


def test_rate_limiter_steady_state_no_extra_sleep() -> None:
    """Calls already spaced further apart than the gap don't cause additional delay."""
    from aoi.pipeline.rate_limit import RateLimiter

    limiter = RateLimiter()
    per_second = 10.0  # 100ms gap
    host = "steady.com"

    # First call — primes the limiter
    limiter.wait(host, per_second)

    # Wait more than the gap
    time.sleep(0.2)

    # Second call — should not sleep (we already waited more than the gap)
    start = time.monotonic()
    limiter.wait(host, per_second)
    elapsed = time.monotonic() - start

    # Should be nearly instant (< 50ms overhead)
    assert elapsed < 0.05, f"Steady-state call took {elapsed:.3f}s, expected < 0.05s"


# ---------------------------------------------------------------------------
# 4. Module-level _limiter is accessible
# ---------------------------------------------------------------------------


def test_rate_limiter_module_level_instance_exists() -> None:
    """The module exposes a module-level _limiter instance of RateLimiter."""
    from aoi.pipeline import rate_limit

    assert hasattr(rate_limit, "_limiter")
    from aoi.pipeline.rate_limit import RateLimiter

    assert isinstance(rate_limit._limiter, RateLimiter)


# ---------------------------------------------------------------------------
# 5. Penalize: pushes back the next-allowed time for a host
# ---------------------------------------------------------------------------


def test_rate_limiter_penalize_delays_next_call() -> None:
    """penalize() pushes back the next request to the penalized host."""
    from aoi.pipeline.rate_limit import RateLimiter

    limiter = RateLimiter()
    host = "penalized.com"

    # Prime the host
    limiter.wait(host, 10.0)

    # Penalize by 0.3 seconds
    limiter.penalize(host, 0.3)

    # Next wait should sleep approximately 0.3s
    start = time.monotonic()
    limiter.wait(host, 10.0)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.25, f"Expected >= 0.25s delay after penalize, got {elapsed:.3f}s"
