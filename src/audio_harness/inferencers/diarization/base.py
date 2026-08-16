from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import NamedTuple


class DiarizationInferencer(ABC):
    class Segment(NamedTuple):
        start_sample: int
        end_sample: int
        speaker: int

    @abstractmethod
    async def ready(self) -> None: ...

    @abstractmethod
    async def probe(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def stream_diarize(
        self,
        audio: bytes,
        sample_rate: int,
    ) -> AsyncIterator[tuple[Segment, ...]]: ...
