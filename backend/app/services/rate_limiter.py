from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(slots=True)
class RateLimitBucket:
    tokens: float
    updated_at: float


class RateLimitExceeded(Exception):
    def __init__(self, action: str, retry_after_seconds: float, rate_per_second: float, burst: int) -> None:
        super().__init__(action)
        self.action = action
        self.retry_after_seconds = retry_after_seconds
        self.rate_per_second = rate_per_second
        self.burst = burst


class TokenBucketRateLimiter:
    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = asyncio.Lock()
        self._buckets: dict[str, RateLimitBucket] = {}

    async def check(self, key: str, *, action: str, rate_per_second: float, burst: int) -> None:
        if rate_per_second <= 0 or burst <= 0:
            return
        now = self._clock()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._buckets[key] = RateLimitBucket(tokens=float(burst - 1), updated_at=now)
                return

            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(float(burst), bucket.tokens + elapsed * rate_per_second)
            bucket.updated_at = now
            if bucket.tokens < 1.0:
                retry_after = (1.0 - bucket.tokens) / rate_per_second
                raise RateLimitExceeded(action, retry_after, rate_per_second, burst)
            bucket.tokens -= 1.0
