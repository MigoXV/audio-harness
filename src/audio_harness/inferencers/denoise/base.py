from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import NamedTuple


class DenoiseInferencer(ABC):
    class SynchronizedChunk(NamedTuple):
        original_pcm_s16le: bytes
        denoised_pcm_s16le: bytes

    @abstractmethod
    async def ready(self) -> None: ...

    @abstractmethod
    async def probe(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def stream_synchronized(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[SynchronizedChunk]: ...
