from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

from audio_harness.engines.models import EngineApi, EngineSnapshot
from audio_harness.errors import EngineConfigurationInvalidError
from audio_harness.inferencers import Inferencer
from audio_harness.inferencers.responses.http import ResponsesInferencer


@dataclass(eq=False)
class _InferencerEntry:
    key: tuple[str, int]
    inferencer: Inferencer
    ready_task: asyncio.Task[None]
    references: int = 0
    retired: bool = False


class InferencerPool:
    """按不可变引擎快照管理推理客户端及其会话租约。"""

    def __init__(
        self,
        *,
        create_inferencer: Callable[[EngineSnapshot], Inferencer],
    ) -> None:
        self._create_inferencer = create_inferencer
        self._entries: dict[tuple[str, int], _InferencerEntry] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def _reserve(self, snapshot: EngineSnapshot) -> _InferencerEntry:
        key = (snapshot.id, snapshot.revision)
        async with self._lock:
            if self._closed:
                raise RuntimeError("推理客户端池已经关闭")
            entry = self._entries.get(key)
            if entry is None or entry.retired:
                inferencer = self._create_inferencer(snapshot)
                entry = _InferencerEntry(
                    key=key,
                    inferencer=inferencer,
                    ready_task=asyncio.create_task(
                        inferencer.ready(),
                        name=f"inferencer-ready-{snapshot.id}-{snapshot.revision}",
                    ),
                )
                self._entries[key] = entry
            entry.references += 1
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
            entry.references -= 1
            if entry.references < 0:
                raise RuntimeError("推理客户端租约引用计数失衡")
            if entry.references == 0 and entry.retired:
                if self._entries.get(entry.key) is entry:
                    del self._entries[entry.key]
                close_inferencer = True
        if close_inferencer:
            await entry.inferencer.close()

    async def prepare(self, snapshot: EngineSnapshot) -> None:
        entry = await self._acquire(snapshot)
        await self._release(entry)

    async def prepare_all(self, snapshots: Iterable[EngineSnapshot]) -> None:
        for snapshot in snapshots:
            await self.prepare(snapshot)

    async def probe(self, snapshot: EngineSnapshot) -> None:
        """使用临时客户端探测引擎，不复用会话运行时缓存。"""

        async with self._lock:
            if self._closed:
                raise RuntimeError("推理客户端池已经关闭")
        inferencer = self._create_inferencer(snapshot)
        try:
            await inferencer.probe()
        finally:
            await inferencer.close()

    async def list_models(self, snapshot: EngineSnapshot) -> tuple[str, ...]:
        """从临时 Responses 客户端读取模型，不污染会话运行时缓存。"""

        if snapshot.api is not EngineApi.RESPONSES:
            raise EngineConfigurationInvalidError(
                "只有 Responses 引擎可以读取模型列表"
            )
        async with self._lock:
            if self._closed:
                raise RuntimeError("推理客户端池已经关闭")
        inferencer = cast(
            ResponsesInferencer,
            self._create_inferencer(snapshot),
        )
        try:
            return await inferencer.list_models()
        finally:
            await inferencer.close()

    @asynccontextmanager
    async def lease(self, snapshots: Iterable[EngineSnapshot]):
        unique = {(snapshot.id, snapshot.revision): snapshot for snapshot in snapshots}
        acquired: list[_InferencerEntry] = []
        try:
            for key in sorted(unique):
                acquired.append(await self._acquire(unique[key]))
            yield {entry.key: entry.inferencer for entry in acquired}
        finally:
            for entry in reversed(acquired):
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
                if entry.references == 0:
                    del self._entries[key]
                    closing.append(entry)
        await asyncio.gather(
            *(entry.inferencer.close() for entry in closing),
            return_exceptions=True,
        )

    async def retire_snapshot(self, asset_id: str, revision: int) -> None:
        key = (asset_id, revision)
        close_inferencer: Inferencer | None = None
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.retired = True
            if entry.references == 0:
                del self._entries[key]
                close_inferencer = entry.inferencer
        if close_inferencer is not None:
            await close_inferencer.close()

    async def readiness(
        self,
        snapshots: Iterable[EngineSnapshot],
    ) -> tuple[bool, list[str]]:
        problems: list[str] = []
        for snapshot in snapshots:
            key = (snapshot.id, snapshot.revision)
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
                problems.append(f"{snapshot.name} 引擎尚未就绪")
        return not problems, problems

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
