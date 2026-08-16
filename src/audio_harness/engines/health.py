from __future__ import annotations

import asyncio
import logging

from audio_harness.engines.catalog import EngineCatalog

logger = logging.getLogger(__name__)


class EngineHealthMonitor:
    """低频探测已启用引擎，并把真实结果持久化到控制面。"""

    def __init__(self, catalog: EngineCatalog) -> None:
        self._catalog = catalog
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="engine-health-monitor",
            )

    async def _run(self) -> None:
        try:
            while not self._closed:
                try:
                    asset_ids = await self._catalog.due_asset_ids()
                    semaphore = asyncio.Semaphore(8)

                    async def probe(asset_id: str) -> None:
                        async with semaphore:
                            if self._closed:
                                return
                            await self._catalog.validate_asset(
                                asset_id,
                                automated=True,
                            )

                    await asyncio.gather(
                        *(probe(asset_id) for asset_id in asset_ids)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("后台引擎健康探测失败")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
