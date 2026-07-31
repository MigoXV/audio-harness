from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from audio_harness.capacity_settings import CapacitySettingsSnapshot
from audio_harness.config import CapacityConfig
from audio_harness.domain import CapacityExceededError


class ResizableSessionLimiter:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.active = 0
        self._lock = asyncio.Lock()

    async def resize(self, capacity: int) -> None:
        async with self._lock:
            self.capacity = capacity

    @asynccontextmanager
    async def slot(self):
        async with self._lock:
            if self.active >= self.capacity:
                raise CapacityExceededError(
                    f"活动会话已达到上限: {self.capacity}"
                )
            self.active += 1
        try:
            yield
        finally:
            async with self._lock:
                self.active -= 1


class ResizableConcurrencyLimiter:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.active = 0
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


class ServingCapacity:
    def __init__(self, config: CapacityConfig) -> None:
        self.grpc_sessions = ResizableSessionLimiter(
            config.grpc.max_sessions
        )
        self.realtime_sessions = ResizableSessionLimiter(
            config.realtime.max_sessions
        )
        self.webrtc_sessions = ResizableSessionLimiter(
            config.webrtc.max_sessions
        )
        self.asr = ResizableConcurrencyLimiter(
            config.max_asr_concurrency
        )

    async def apply(self, snapshot: CapacitySettingsSnapshot) -> None:
        await asyncio.gather(
            self.grpc_sessions.resize(
                snapshot.config.grpc.max_sessions
            ),
            self.realtime_sessions.resize(
                snapshot.config.realtime.max_sessions
            ),
            self.webrtc_sessions.resize(
                snapshot.config.webrtc.max_sessions
            ),
            self.asr.resize(snapshot.config.max_asr_concurrency),
        )
