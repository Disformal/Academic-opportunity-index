"""Per-host token bucket rate limiter for AOI."""

import threading
import time


class RateLimiter:
    """Per-host token bucket. Thread-safe."""

    def __init__(self) -> None:
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, per_second: float) -> None:
        """Block until the next request to this host is allowed."""
        gap = 1.0 / per_second
        with self._lock:
            now = time.monotonic()
            earliest = self._next_allowed.get(host, 0.0)
            delay = max(0.0, earliest - now)
            self._next_allowed[host] = max(now, earliest) + gap
        if delay > 0:
            time.sleep(delay)

    def penalize(self, host: str, seconds: float) -> None:
        """Push back the next-allowed time for a host.

        Called after a 429 to slow down subsequent requests.
        """
        with self._lock:
            now = time.monotonic()
            current = self._next_allowed.get(host, now)
            self._next_allowed[host] = max(now, current) + seconds


_limiter = RateLimiter()
