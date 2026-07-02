from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Async token bucket. acquire() blocks until a token is available."""

    def __init__(self, rate_per_sec: float, capacity: int | None = None) -> None:
        self.rate = rate_per_sec
        self.capacity = capacity or max(1, int(rate_per_sec))
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)
