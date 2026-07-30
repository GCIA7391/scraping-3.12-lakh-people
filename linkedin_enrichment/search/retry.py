"""Retry policy with exponential backoff and jitter.

Two rules govern what is worth retrying:

* Transient (429, 202, 5xx, timeouts) — retry, and tell the rate limiter to slow
  down, because on a free provider a throttle means the whole run is going too
  fast, not that this one request was unlucky.
* Terminal (401, 403, 400) — fail immediately. A bad API key will not fix itself,
  and retrying it four times per row across 312,160 rows wastes hours before the
  operator sees the problem.

Jitter is full-random rather than a fixed multiplier: with several workers
throttled simultaneously, deterministic backoff would resynchronise them into a
thundering herd on every retry.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

from ..providers.base import ProviderError

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def with_retry(
    operation: Callable[[], Awaitable[T]],
    config,
    *,
    on_throttle: Callable[[], None] | None = None,
    description: str = "request",
) -> T:
    """Run ``operation``, retrying transient failures per ``config``."""
    last_error: Exception | None = None

    for attempt in range(1, config.max_attempts + 1):
        try:
            return await operation()
        except ProviderError as exc:
            last_error = exc
            if exc.status in (202, 429) and on_throttle is not None:
                on_throttle()
            if not exc.retryable or attempt >= config.max_attempts:
                raise
            delay = _backoff_delay(attempt, config)
            logger.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                description, attempt, config.max_attempts, exc, delay,
            )
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapters may raise anything
            last_error = exc
            if attempt >= config.max_attempts:
                raise
            delay = _backoff_delay(attempt, config)
            logger.warning(
                "%s raised %s (attempt %d/%d) — retrying in %.1fs",
                description, exc, attempt, config.max_attempts, delay,
            )
            await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error


def _backoff_delay(attempt: int, config) -> float:
    """Exponential backoff with full jitter, capped at ``max_delay_seconds``."""
    base = min(config.base_delay_seconds * (2 ** (attempt - 1)), config.max_delay_seconds)
    spread = base * config.jitter
    return max(0.0, base + random.uniform(-spread, spread))
