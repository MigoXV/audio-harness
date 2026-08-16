from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.configs import (
    CapacityConfig,
    ProtocolCapacityConfig,
)
from audio_harness.database.models import CapacitySetting, utc_now

_SETTING_ID = 1


class CapacitySettingsError(Exception):
    code = "capacity_settings_error"


class CapacityRevisionConflictError(CapacitySettingsError):
    code = "capacity_revision_conflict"


@dataclass(frozen=True)
class CapacitySettingsSnapshot:
    config: CapacityConfig
    revision: int
    updated_at: datetime


SettingsListener = Callable[
    [CapacitySettingsSnapshot],
    Awaitable[None],
]


def _config_from_model(model: CapacitySetting) -> CapacityConfig:
    return CapacityConfig(
        grpc=ProtocolCapacityConfig(
            max_sessions=model.grpc_max_sessions
        ),
        realtime=ProtocolCapacityConfig(
            max_sessions=model.realtime_max_sessions
        ),
        webrtc=ProtocolCapacityConfig(
            max_sessions=model.webrtc_max_sessions
        ),
        max_asr_concurrency=model.max_asr_concurrency,
        max_embedding_concurrency=model.max_embedding_concurrency,
        max_qdrant_concurrency=model.max_qdrant_concurrency,
        max_responses_concurrency=model.max_responses_concurrency,
        max_tts_concurrency=model.max_tts_concurrency,
    )


def _snapshot(model: CapacitySetting) -> CapacitySettingsSnapshot:
    return CapacitySettingsSnapshot(
        config=_config_from_model(model),
        revision=model.revision,
        updated_at=model.updated_at,
    )


class CapacitySettingsStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory
        self._current: CapacitySettingsSnapshot | None = None
        self._lock = asyncio.Lock()
        self._listeners: list[SettingsListener] = []

    def subscribe(self, listener: SettingsListener) -> None:
        self._listeners.append(listener)

    async def initialize(self) -> CapacitySettingsSnapshot:
        async with self._lock:
            if self._current is not None:
                return self._current
            async with self._session_factory() as session:
                model = await session.get(CapacitySetting, _SETTING_ID)
                if model is None:
                    raise RuntimeError(
                        "数据库缺少 capacity_settings 单例记录"
                    )
                self._current = _snapshot(model)
                return self._current

    async def get(self) -> CapacitySettingsSnapshot:
        if self._current is None:
            return await self.initialize()
        return self._current

    def current(self) -> CapacitySettingsSnapshot:
        if self._current is None:
            raise RuntimeError("容量设置尚未初始化")
        return self._current

    async def update(
        self,
        *,
        config: CapacityConfig,
        expected_revision: int,
    ) -> CapacitySettingsSnapshot:
        async with self._lock:
            async with self._session_factory() as session:
                model = await session.get(CapacitySetting, _SETTING_ID)
                if model is None:
                    raise RuntimeError(
                        "数据库缺少 capacity_settings 单例记录"
                    )
                if model.revision != expected_revision:
                    raise CapacityRevisionConflictError(
                        "容量设置已被其他操作更新，请重新加载"
                    )
                model.grpc_max_sessions = config.grpc.max_sessions
                model.realtime_max_sessions = (
                    config.realtime.max_sessions
                )
                model.webrtc_max_sessions = config.webrtc.max_sessions
                model.max_asr_concurrency = (
                    config.max_asr_concurrency
                )
                model.max_embedding_concurrency = config.max_embedding_concurrency
                model.max_qdrant_concurrency = config.max_qdrant_concurrency
                model.max_responses_concurrency = (
                    config.max_responses_concurrency
                )
                model.max_tts_concurrency = config.max_tts_concurrency
                model.revision += 1
                model.updated_at = utc_now()
                await session.commit()
                await session.refresh(model)
                next_snapshot = _snapshot(model)
            self._current = next_snapshot
            for listener in self._listeners:
                await listener(next_snapshot)
            return next_snapshot
