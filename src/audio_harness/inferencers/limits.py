from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from audio_harness.errors import CapacityExceededError


class ResizableConcurrencyLimiter:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.active = 0
        self.rejected = 0
        self._condition = asyncio.Condition()

    async def resize(self, capacity: int) -> None:
        async with self._condition:
            self.capacity = capacity
            self._condition.notify_all()

    async def __aenter__(self) -> "ResizableConcurrencyLimiter":
        async with self._condition:
            await self._condition.wait_for(
                lambda: self.active < self.capacity
            )
            self.active += 1
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        async with self._condition:
            self.active -= 1
            self._condition.notify_all()

    @asynccontextmanager
    async def try_slot(self):
        async with self._condition:
            if self.active >= self.capacity:
                self.rejected += 1
                raise CapacityExceededError(
                    f"并发推理已达到上限: {self.capacity}"
                )
            self.active += 1
        try:
            yield self
        finally:
            async with self._condition:
                self.active -= 1
                self._condition.notify_all()
