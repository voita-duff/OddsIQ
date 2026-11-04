"""Shared async retry with exponential backoff + full jitter.

An ``attempt`` coroutine signals a retryable failure by raising
``RetryableError`` (optionally carrying a server ``Retry-After``). Any other
exception propagates immediately. Used by the two Odds API callers
(PinnacleScraper, OddsApiScheduleSource) so a transient 429/5xx/transport
error retries instead of silently dropping a poll.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_ATTEMPTS = 3
BASE_DELAY = 1.0
MAX_DELAY = 30.0


class RetryableError(Exception):
    """Raised by an attempt to request a retry. ``retry_after`` (seconds),
    when set, overrides the computed backoff for that wait (clamped to
    ``max_delay`` so a hostile/buggy header can't stall the pipeline)."""

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(value: str | None) -> float | None:
    """Parse an HTTP ``Retry-After`` delta-seconds value. HTTP-date form and
    garbage return ``None`` (the Odds API uses seconds)."""
    if not value:
        return None
    try:
        secs = float(value.strip())
    except ValueError:
        return None
    return secs if secs >= 0 else None


def _backoff_delay(attempt: int, base_delay: float, max_delay: float,
                   rng: Callable[[], float]) -> float:
    """Full-jitter exponential backoff: uniform in [0, min(max_delay,
    base*2**(attempt-1)))."""
    capped = min(max_delay, base_delay * (2 ** (attempt - 1)))
    return capped * rng()


async def with_backoff(
    attempt: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = MAX_ATTEMPTS,
    base_delay: float = BASE_DELAY,
    max_delay: float = MAX_DELAY,
    rng: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    label: str = "request",
) -> T:
    """Call ``attempt`` until it returns, retrying on ``RetryableError`` with
    backoff. Re-raises the final ``RetryableError`` after ``max_attempts``.
    ``rng``/``sleep`` are injectable for tests."""
    _rng = rng if rng is not None else random.random
    _sleep = sleep if sleep is not None else asyncio.sleep
    for n in range(1, max_attempts + 1):
        try:
            return await attempt()
        except RetryableError as e:
            if n >= max_attempts:
                logger.warning("%s: giving up after %d attempt(s)", label, n)
                raise
            delay = (
                min(e.retry_after, max_delay) if e.retry_after is not None
                else _backoff_delay(n, base_delay, max_delay, _rng)
            )
            logger.warning(
                "%s: attempt %d failed (%s); retrying in %.2fs", label, n, e, delay,
            )
            await _sleep(delay)
    raise RuntimeError("with_backoff: unreachable")  # pragma: no cover
