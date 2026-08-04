from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.assets import EngineSnapshot
from audio_harness.inferencers.contracts import (
    InferencerFactory,
    ManagedInferencer,
)

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
        del session_factory
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

    async def prepare_all(
        self,
        snapshots: Iterable[EngineSnapshot],
    ) -> None:
        for snapshot in snapshots:
            await self.prepare(snapshot)

    async def probe(self, snapshot: EngineSnapshot) -> None:
        """使用临时客户端探测引擎，绝不复用会话运行时缓存。"""
        async with self._lock:
            if self._closed:
                raise RuntimeError("引擎路由器已经关闭")
        inferencer = self._inferencer_factory.create(snapshot)
        try:
            await inferencer.probe()
        finally:
            await inferencer.close()

    @asynccontextmanager
    async def lease_snapshots(
        self,
        snapshots: Iterable[EngineSnapshot],
    ):
        unique = {
            (snapshot.id, snapshot.revision): snapshot
            for snapshot in snapshots
        }
        acquired: list[_InferencerEntry] = []
        try:
            for key in sorted(unique):
                acquired.append(await self._acquire(unique[key]))
            yield {
                entry.key: entry.inferencer
                for entry in acquired
            }
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
                if entry.refs == 0:
                    del self._entries[key]
                    closing.append(entry)
        await asyncio.gather(
            *(entry.inferencer.close() for entry in closing),
            return_exceptions=True,
        )

    async def retire_snapshot(
        self,
        asset_id: str,
        revision: int,
    ) -> None:
        key = (asset_id, revision)
        close_inferencer: ManagedInferencer | None = None
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.retired = True
            if entry.refs == 0:
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
