from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.configs import WorkflowRuntimeConfig
from audio_harness.database.models import WorkflowSetting, utc_now

_SETTING_ID = 1


class WorkflowSettingsError(Exception):
    code = "workflow_settings_error"


class WorkflowRevisionConflictError(WorkflowSettingsError):
    code = "workflow_revision_conflict"


class WorkflowSettingsValidationError(WorkflowSettingsError):
    code = "workflow_settings_invalid"


@dataclass(frozen=True)
class WorkflowSettingsSnapshot:
    config: WorkflowRuntimeConfig
    grpc_audio_workflow_id: str | None
    revision: int
    updated_at: datetime


SettingsListener = Callable[
    [WorkflowSettingsSnapshot],
    Awaitable[None],
]


def _config_from_model(model: WorkflowSetting) -> WorkflowRuntimeConfig:
    return WorkflowRuntimeConfig(
        min_chunk_bytes=model.min_chunk_bytes,
        default_language_code=model.default_language_code,
        audio_queue_capacity=model.audio_queue_capacity,
        segment_queue_capacity=model.segment_queue_capacity,
        result_queue_capacity=model.result_queue_capacity,
    )


def _snapshot(model: WorkflowSetting) -> WorkflowSettingsSnapshot:
    return WorkflowSettingsSnapshot(
        config=_config_from_model(model),
        grpc_audio_workflow_id=model.grpc_audio_workflow_id,
        revision=model.revision,
        updated_at=model.updated_at,
    )


class WorkflowSettingsStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_message_bytes: int,
    ) -> None:
        self._session_factory = session_factory
        self._max_message_bytes = max_message_bytes
        self._current: WorkflowSettingsSnapshot | None = None
        self._lock = asyncio.Lock()
        self._listeners: list[SettingsListener] = []

    def subscribe(self, listener: SettingsListener) -> None:
        self._listeners.append(listener)

    async def initialize(self) -> WorkflowSettingsSnapshot:
        async with self._lock:
            if self._current is not None:
                return self._current
            async with self._session_factory() as session:
                model = await session.get(WorkflowSetting, _SETTING_ID)
                if model is None:
                    raise RuntimeError("数据库缺少 workflow_settings 单例记录")
                self._validate_message_limit(_config_from_model(model))
                self._current = _snapshot(model)
                return self._current

    async def get(self) -> WorkflowSettingsSnapshot:
        if self._current is None:
            return await self.initialize()
        return self._current

    def current(self) -> WorkflowSettingsSnapshot:
        if self._current is None:
            raise RuntimeError("工作流设置尚未初始化")
        return self._current

    def _validate_message_limit(
        self,
        config: WorkflowRuntimeConfig,
    ) -> None:
        if config.min_chunk_bytes > self._max_message_bytes:
            raise WorkflowSettingsValidationError(
                "min_chunk_bytes 不能大于 max_message_bytes"
            )

    async def update(
        self,
        *,
        config: WorkflowRuntimeConfig,
        expected_revision: int,
    ) -> WorkflowSettingsSnapshot:
        self._validate_message_limit(config)
        async with self._lock:
            async with self._session_factory() as session:
                model = await session.get(WorkflowSetting, _SETTING_ID)
                if model is None:
                    raise RuntimeError("数据库缺少 workflow_settings 单例记录")
                if model.revision != expected_revision:
                    raise WorkflowRevisionConflictError(
                        "工作流设置已被其他操作更新，请重新加载"
                    )
                for field, value in config.model_dump().items():
                    setattr(model, field, value)
                model.revision += 1
                model.updated_at = utc_now()
                await session.commit()
                await session.refresh(model)
                next_snapshot = _snapshot(model)
            self._current = next_snapshot
            for listener in self._listeners:
                await listener(next_snapshot)
            return next_snapshot

    async def set_grpc_audio_workflow(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
    ) -> WorkflowSettingsSnapshot:
        async with self._lock:
            async with self._session_factory() as session:
                model = await session.get(WorkflowSetting, _SETTING_ID)
                if model is None:
                    raise RuntimeError("数据库缺少 workflow_settings 单例记录")
                if model.revision != expected_revision:
                    raise WorkflowRevisionConflictError(
                        "工作流设置已被其他操作更新，请重新加载"
                    )
                model.grpc_audio_workflow_id = workflow_id
                model.revision += 1
                model.updated_at = utc_now()
                await session.commit()
                await session.refresh(model)
                next_snapshot = _snapshot(model)
            self._current = next_snapshot
            for listener in self._listeners:
                await listener(next_snapshot)
            return next_snapshot
