from __future__ import annotations

from typing import Protocol, TypeAlias

from audio_harness.assets import EngineSnapshot
from audio_harness.domain import (
    AsyncAsrEngine,
    AsyncResponsesEngine,
    AsyncTtsEngine,
    AsyncVadEngine,
)


class ManagedAsrInferencer(AsyncAsrEngine, Protocol):
    async def ready(self) -> None: ...

    async def probe(self) -> None: ...

    async def close(self) -> None: ...


class ManagedVadInferencer(AsyncVadEngine, Protocol):
    async def ready(self) -> None: ...

    async def probe(self) -> None: ...

    async def close(self) -> None: ...


class ManagedResponsesInferencer(AsyncResponsesEngine, Protocol):
    async def ready(self) -> None: ...

    async def probe(self) -> None: ...

    async def close(self) -> None: ...


class ManagedTtsInferencer(AsyncTtsEngine, Protocol):
    async def ready(self) -> None: ...

    async def probe(self) -> None: ...

    async def close(self) -> None: ...


ManagedInferencer: TypeAlias = (
    ManagedAsrInferencer
    | ManagedVadInferencer
    | ManagedResponsesInferencer
    | ManagedTtsInferencer
)


class InferencerFactory(Protocol):
    def create(self, snapshot: EngineSnapshot) -> ManagedInferencer: ...
