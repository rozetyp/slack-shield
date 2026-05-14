"""Tests for the in-memory async rate limiter."""

import asyncio
import time

import pytest

from slack_shield import RateLimiter


async def test_first_acquire_is_immediate():
    rl = RateLimiter(interval_seconds=60.0)
    t0 = time.monotonic()
    await rl.acquire("ws", "history")
    assert time.monotonic() - t0 < 0.05


async def test_second_acquire_waits_the_interval():
    rl = RateLimiter(interval_seconds=0.2)
    await rl.acquire("ws", "history")
    t0 = time.monotonic()
    await rl.acquire("ws", "history")
    elapsed = time.monotonic() - t0
    assert 0.18 <= elapsed <= 0.35, f"expected ~0.2s, got {elapsed:.3f}"


async def test_different_routes_are_independent():
    rl = RateLimiter(interval_seconds=60.0)
    await rl.acquire("ws", "history")
    t0 = time.monotonic()
    await rl.acquire("ws", "replies")
    assert time.monotonic() - t0 < 0.05


async def test_different_workspaces_are_independent():
    rl = RateLimiter(interval_seconds=60.0)
    await rl.acquire("ws_a", "history")
    t0 = time.monotonic()
    await rl.acquire("ws_b", "history")
    assert time.monotonic() - t0 < 0.05


async def test_note_retry_after_pushes_bucket_forward():
    rl = RateLimiter(interval_seconds=0.1)
    await rl.acquire("ws", "history")
    await rl.note_retry_after("ws", "history", 0.3)
    t0 = time.monotonic()
    await rl.acquire("ws", "history")
    elapsed = time.monotonic() - t0
    assert 0.27 <= elapsed <= 0.45, f"expected ~0.3s, got {elapsed:.3f}"


async def test_retry_after_does_not_shorten_wait():
    """If the bucket already says wait 2s, a retry_after of 0.1s shouldn't shrink it."""
    rl = RateLimiter(interval_seconds=2.0)
    await rl.acquire("ws", "history")
    await rl.note_retry_after("ws", "history", 0.1)
    remaining = rl.time_until_next("ws", "history")
    assert remaining > 1.5, f"interval should win, got {remaining:.3f}"


async def test_time_until_next_before_first_acquire_is_zero():
    rl = RateLimiter(interval_seconds=60.0)
    assert rl.time_until_next("ws", "history") == 0.0


async def test_concurrent_acquires_serialize():
    """Two coroutines acquiring the same bucket: second waits for first."""
    rl = RateLimiter(interval_seconds=0.2)
    t0 = time.monotonic()

    async def grab():
        await rl.acquire("ws", "history")
        return time.monotonic() - t0

    times = await asyncio.gather(grab(), grab())
    times.sort()
    assert times[0] < 0.05
    assert times[1] >= 0.18, f"second grab should wait ~0.2s, got {times[1]:.3f}"
