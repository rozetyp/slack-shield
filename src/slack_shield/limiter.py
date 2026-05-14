"""In-memory async rate limiter for Slack's 1 req/min limit on unlisted apps.

The limiter is keyed by (workspace, route) and enforces a minimum interval
between successive acquire() calls. When Slack returns 429 with Retry-After,
call note_retry_after() so the next wait honors Slack's instruction instead
of just the base interval.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    next_allowed_at: float = 0.0


class RateLimiter:
    def __init__(self, interval_seconds: float = 60.0):
        self._interval = interval_seconds
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, workspace: str, route: str) -> None:
        """Block until a token is available for (workspace, route)."""
        key = (workspace, route)
        while True:
            async with self._lock:
                bucket = self._buckets.setdefault(key, _Bucket())
                now = time.monotonic()
                wait = bucket.next_allowed_at - now
                if wait <= 0:
                    bucket.next_allowed_at = now + self._interval
                    return
            await asyncio.sleep(wait)

    async def note_retry_after(self, workspace: str, route: str, retry_after: float) -> None:
        """Slack told us to wait `retry_after` seconds. Push the bucket forward."""
        key = (workspace, route)
        async with self._lock:
            bucket = self._buckets.setdefault(key, _Bucket())
            now = time.monotonic()
            bucket.next_allowed_at = max(bucket.next_allowed_at, now + retry_after)

    def time_until_next(self, workspace: str, route: str) -> float:
        """Return seconds until next acquire would succeed (0 if ready)."""
        key = (workspace, route)
        bucket = self._buckets.get(key)
        if bucket is None:
            return 0.0
        return max(0.0, bucket.next_allowed_at - time.monotonic())
