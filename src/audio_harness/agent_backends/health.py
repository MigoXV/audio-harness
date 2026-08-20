from __future__ import annotations

import asyncio
import logging

from audio_harness.agent_backends.catalog import AgentBackendCatalog

logger = logging.getLogger(__name__)


class AgentBackendHealthMonitor:
    def __init__(self, catalog: AgentBackendCatalog) -> None:
        self._catalog = catalog
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="agent-backend-health-monitor",
            )

    async def _run(self) -> None:
        try:
            while not self._closed:
                try:
                    ids = await self._catalog.due_asset_ids()
                    semaphore = asyncio.Semaphore(4)

                    async def probe(asset_id: str) -> None:
                        async with semaphore:
                            if not self._closed:
                                await self._catalog.validate_asset(
                                    asset_id,
                                    automated=True,
                                )

                    await asyncio.gather(*(probe(asset_id) for asset_id in ids))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("后台 Agent Backend 健康探测失败")
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
