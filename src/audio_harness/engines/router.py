from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from audio_harness.assets import EngineRole, EngineSnapshot
from audio_harness.database.models import EngineAsset, EngineBinding
from audio_harness.domain import (
    AsyncAsrEngine,
    AsyncVadEngine,
    EngineBindingInvalidError,
    EngineBindingMissingError,
    EngineSet,
)
from audio_harness.inferencers.contracts import (
    InferencerFactory,
    ManagedInferencer,
)

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class _InferencerEntry:
    key: tuple[str, int]
    inferencer: ManagedInferencer
    ready_task: asyncio.Task[None]
    refs: int = 0
    retired: bool = False


class EngineRouter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        inferencer_factory: InferencerFactory,
    ) -> None:
        self._session_factory = session_factory
        self._inferencer_factory = inferencer_factory
        self._entries: dict[tuple[str, int], _InferencerEntry] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def _reserve(self, snapshot: EngineSnapshot) -> _InferencerEntry:
        key = (snapshot.id, snapshot.revision)
        async with self._lock:
            if self._closed:
                raise RuntimeError("引擎路由器已经关闭")
            entry = self._entries.get(key)
            if entry is None or entry.retired:
                inferencer = self._inferencer_factory.create(snapshot)
                entry = _InferencerEntry(
                    key=key,
                    inferencer=inferencer,
                    ready_task=asyncio.create_task(
                        inferencer.ready(),
                        name=f"engine-ready-{snapshot.id}-{snapshot.revision}",
                    ),
                )
                self._entries[key] = entry
            entry.refs += 1
            return entry

    async def _acquire(self, snapshot: EngineSnapshot) -> _InferencerEntry:
        entry = await self._reserve(snapshot)
        try:
            await asyncio.shield(entry.ready_task)
        except BaseException:
            if entry.ready_task.done() and not entry.ready_task.cancelled():
                try:
                    entry.ready_task.result()
                except Exception:
                    entry.retired = True
            await self._release(entry)
            raise
        return entry

    async def _release(self, entry: _InferencerEntry) -> None:
        close_inferencer = False
        async with self._lock:
            entry.refs -= 1
            if entry.refs < 0:
                raise RuntimeError("引擎客户端租约引用计数失衡")
            if entry.refs == 0 and entry.retired:
                if self._entries.get(entry.key) is entry:
                    del self._entries[entry.key]
                close_inferencer = True
        if close_inferencer:
            await entry.inferencer.close()

    async def prepare(self, snapshot: EngineSnapshot) -> None:
        entry = await self._acquire(snapshot)
        await self._release(entry)

    async def retire(
        self,
        asset_id: str,
        *,
        before_revision: int | None = None,
    ) -> None:
        closing: list[_InferencerEntry] = []
        async with self._lock:
            for key, entry in list(self._entries.items()):
                if key[0] != asset_id:
                    continue
                if before_revision is not None and key[1] >= before_revision:
                    continue
                entry.retired = True
                if entry.refs == 0:
                    del self._entries[key]
                    closing.append(entry)
        await asyncio.gather(
            *(entry.inferencer.close() for entry in closing),
            return_exceptions=True,
        )

    async def _load_bindings(self) -> dict[EngineRole, EngineAsset]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(EngineBinding).options(selectinload(EngineBinding.engine))
            )
            return {
                binding.role: binding.engine
                for binding in result.scalars().all()
            }

    @staticmethod
    def _validate_binding(role: EngineRole, asset: EngineAsset) -> None:
        if not asset.enabled:
            raise EngineBindingInvalidError(
                f"{role.value} 绑定的引擎已禁用: {asset.name}"
            )
        if asset.api is not role.required_api:
            raise EngineBindingInvalidError(
                f"{role.value} 需要 {role.required_api.value}，"
                f"实际为 {asset.api.value}"
            )

    @staticmethod
    def _snapshot(asset: EngineAsset) -> EngineSnapshot:
        return EngineSnapshot(
            id=asset.id,
            revision=asset.revision,
            name=asset.name,
            api=asset.api,
            protocol=asset.protocol,
            target=asset.target,
            connect_timeout_seconds=asset.connect_timeout_seconds,
            request_timeout_seconds=asset.request_timeout_seconds,
        )

    async def prewarm(self) -> None:
        bindings = await self._load_bindings()
        for role, asset in bindings.items():
            try:
                self._validate_binding(role, asset)
                await self.prepare(self._snapshot(asset))
            except Exception:
                logger.warning(
                    "预热引擎绑定失败: role=%s, engine=%s",
                    role.value,
                    asset.name,
                    exc_info=True,
                )

    async def readiness(self) -> tuple[bool, list[str]]:
        bindings = await self._load_bindings()
        problems: list[str] = []
        for role in (EngineRole.ASR, EngineRole.VAD):
            asset = bindings.get(role)
            if asset is None:
                problems.append(f"缺少 {role.value} 绑定")
                continue
            try:
                self._validate_binding(role, asset)
            except EngineBindingInvalidError as exc:
                problems.append(str(exc))
                continue
            key = (asset.id, asset.revision)
            async with self._lock:
                entry = self._entries.get(key)
                prepared = (
                    entry is not None
                    and not entry.retired
                    and entry.ready_task.done()
                    and not entry.ready_task.cancelled()
                    and entry.ready_task.exception() is None
                )
            if not prepared:
                problems.append(f"{role.value} 引擎尚未就绪")
        return not problems, problems

    @asynccontextmanager
    async def lease(self):
        bindings = await self._load_bindings()
        missing = [
            role.value
            for role in (EngineRole.ASR, EngineRole.VAD)
            if role not in bindings
        ]
        if missing:
            raise EngineBindingMissingError(
                f"缺少运行时引擎绑定: {', '.join(missing)}"
            )

        snapshots: dict[EngineRole, EngineSnapshot] = {}
        for role, asset in bindings.items():
            self._validate_binding(role, asset)
            snapshots[role] = self._snapshot(asset)

        acquired: list[_InferencerEntry] = []
        try:
            for role in (
                EngineRole.ASR,
                EngineRole.VAD,
                EngineRole.SEMANTIC_VAD,
            ):
                snapshot = snapshots.get(role)
                if snapshot is not None:
                    acquired.append(await self._acquire(snapshot))

            by_role: dict[EngineRole, ManagedInferencer] = {}
            index = 0
            for role in (
                EngineRole.ASR,
                EngineRole.VAD,
                EngineRole.SEMANTIC_VAD,
            ):
                if role in snapshots:
                    by_role[role] = acquired[index].inferencer
                    index += 1

            asr = cast(AsyncAsrEngine, by_role[EngineRole.ASR])
            vad = cast(AsyncVadEngine, by_role[EngineRole.VAD])
            semantic = by_role.get(EngineRole.SEMANTIC_VAD)
            semantic_vad = (
                cast(AsyncVadEngine, semantic)
                if semantic is not None
                else None
            )
            yield EngineSet(
                asr=asr,
                vad=vad,
                semantic_vad=semantic_vad,
            )
        finally:
            for entry in reversed(acquired):
                await self._release(entry)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = list(self._entries.values())
            self._entries.clear()
            for entry in entries:
                entry.retired = True
                if not entry.ready_task.done():
                    entry.ready_task.cancel()
        await asyncio.gather(
            *(entry.ready_task for entry in entries),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(entry.inferencer.close() for entry in entries),
            return_exceptions=True,
        )
