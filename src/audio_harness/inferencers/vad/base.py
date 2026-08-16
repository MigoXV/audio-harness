from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from typing import NamedTuple, TypeAlias, TypedDict


class VadInferencer(ABC):
    Boundary: TypeAlias = tuple[int | None, int | None]

    class Update(NamedTuple):
        boundaries: tuple[VadInferencer.Boundary, ...]
        processed_samples: int

    class Postprocessing(TypedDict):
        processor: str
        version: int
        parameters: Mapping[str, int | float]

    @abstractmethod
    async def ready(self) -> None: ...

    @abstractmethod
    async def probe(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
        *,
        postprocessing: Postprocessing | None = None,
    ) -> AsyncIterator[Update]: ...

    @abstractmethod
    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool: ...
