from __future__ import annotations

import pytest

from oddsiq.scrapers.retry import RetryableError, parse_retry_after, with_backoff


@pytest.mark.asyncio
async def test_returns_on_success_without_sleeping():
    slept = []

    async def _sleep(d):
        slept.append(d)

    async def attempt():
        return 42

    assert await with_backoff(attempt, sleep=_sleep) == 42
    assert slept == []


@pytest.mark.asyncio
async def test_retries_then_reraises():
    slept = []

    async def _sleep(d):
        slept.append(d)

    calls = []

    async def attempt():
        calls.append(1)
        raise RetryableError("nope")

    with pytest.raises(RetryableError):
        await with_backoff(
            attempt, max_attempts=3, base_delay=1.0, rng=lambda: 0.5, sleep=_sleep,
        )
    assert len(calls) == 3
    assert slept == [0.5, 1.0]  # full jitter: 1*2^0*0.5, then 1*2^1*0.5


@pytest.mark.asyncio
async def test_honors_retry_after():
    slept = []

    async def _sleep(d):
        slept.append(d)

    state = {"n": 0}

    async def attempt():
        state["n"] += 1
        if state["n"] == 1:
            raise RetryableError("rate limited", retry_after=7.0)
        return "ok"

    assert await with_backoff(attempt, rng=lambda: 0.5, sleep=_sleep) == "ok"
    assert slept == [7.0]


@pytest.mark.asyncio
async def test_retry_after_is_capped_at_max_delay():
    # A hostile/buggy server header must not stall the single sequential
    # scheduler chain for an unbounded time: cap the honored Retry-After.
    slept = []

    async def _sleep(d):
        slept.append(d)

    state = {"n": 0}

    async def attempt():
        state["n"] += 1
        if state["n"] == 1:
            raise RetryableError("rate limited", retry_after=3600.0)
        return "ok"

    assert await with_backoff(
        attempt, max_delay=30.0, rng=lambda: 0.5, sleep=_sleep,
    ) == "ok"
    assert slept == [30.0]


@pytest.mark.asyncio
async def test_non_retryable_propagates_immediately():
    async def attempt():
        raise ValueError("hard error")

    with pytest.raises(ValueError):
        await with_backoff(attempt, sleep=lambda d: None)


def test_parse_retry_after():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(" 0 ") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert parse_retry_after("-3") is None
