from __future__ import annotations

from typing import Protocol, TypeAlias

from audio_harness.assets import EngineSnapshot
from audio_harness.domain import AsyncAsrEngine, AsyncVadEngine


class ManagedAsrInferencer(AsyncAsrEngine, Protocol):
    async def ready(self) -> None: ...

    async def close(self) -> None: ...


class ManagedVadInferencer(AsyncVadEngine, Protocol):
    async def ready(self) -> None: ...

    async def close(self) -> None: ...


ManagedInferencer: TypeAlias = ManagedAsrInferencer | ManagedVadInferencer


class InferencerFactory(Protocol):
    def create(self, snapshot: EngineSnapshot) -> ManagedInferencer: ...
