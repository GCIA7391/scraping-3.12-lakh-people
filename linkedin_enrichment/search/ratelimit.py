"""Adaptive token-bucket rate limiter.

A fixed rate limit is wrong for this workload. The default providers are free —
a self-hosted SearXNG proxying real search engines, or DuckDuckGo — and both
degrade rather than fail: they start answering with throttle responses when
pushed. A static limit is therefore either too slow (wasting days on a 312k run)
or too fast (getting the instance CAPTCHA-walled).

So the bucket adapts. Every throttle signal halves the refill rate; every success
nudges it back up. The run settles at whatever rate the provider will actually
sustain, without supervision.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class AdaptiveRateLimiter:
    """Async token bucket that shrinks under throttling and recovers on success."""

    def __init__(self, config) -> None:
        self._configured_rate = float(config.requests_per_second)
        self._rate = float(config.requests_per_second)
        self._burst = max(1, int(config.burst))
        self._backoff_factor = float(config.throttle_backoff_factor)
        self._recovery_factor = float(config.recovery_factor)
        self._min_rate = float(config.min_requests_per_second)

        self._tokens = float(self._burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

        self.throttle_events = 0

    @property
    def rate(self) -> float:
        return self._rate

    async def acquire(self) -> None:
        """Block until a request may be issued."""
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Compute the wait inside the lock, sleep outside it, so one
                # waiter cannot block the others from refilling.
                deficit = 1.0 - self._tokens
                wait = deficit / self._rate if self._rate > 0 else 1.0
            await asyncio.sleep(min(wait, 30.0))

    def on_throttled(self) -> None:
        """Called after a 429 / 202 / rate-limit error."""
        self.throttle_events += 1
        previous = self._rate
        self._rate = max(self._min_rate, self._rate * self._backoff_factor)
        # Drop any accumulated burst allowance — continuing to spend it would
        # keep hammering a provider that has just asked us to stop.
        self._tokens = 0.0
        if self._rate < previous:
            logger.warning(
                "provider throttled; rate %.3f -> %.3f req/s (%d throttle events)",
                previous, self._rate, self.throttle_events,
            )

    def on_success(self) -> None:
        """Called after a successful request; slowly restores the configured rate."""
        if self._rate < self._configured_rate:
            self._rate = min(self._configured_rate, self._rate * self._recovery_factor)
