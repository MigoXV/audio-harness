from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from audio_harness.errors import CapacityExceededError


class ResizableSessionLimiter:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.active = 0
        self.rejected = 0
        self._lock = asyncio.Lock()

    async def resize(self, capacity: int) -> None:
        async with self._lock:
            self.capacity = capacity

    @asynccontextmanager
    async def slot(self):
        async with self._lock:
            if self.active >= self.capacity:
                self.rejected += 1
                raise CapacityExceededError(
                    f"活动会话已达到上限: {self.capacity}"
                )
            self.active += 1
        try:
            yield
        finally:
            async with self._lock:
                self.active -= 1
