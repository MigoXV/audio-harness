from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import NamedTuple, TypeAlias


class TtsInferencer(ABC):
    class ReferenceVoice(NamedTuple):
        pcm_s16le: bytes
        sample_rate: int
        ref_text: str = ""

    Voice: TypeAlias = str | ReferenceVoice

    class Chunk(NamedTuple):
        pcm_s16le: bytes
        sample_rate: int
        chunk_index: int

    @abstractmethod
    async def ready(self) -> None: ...

    @abstractmethod
    async def probe(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def duplex_synthesize(
        self,
        text_chunks: AsyncIterator[str],
        *,
        voice: Voice,
        language: str,
        decoder_chunk_size: int,
    ) -> AsyncIterator[Chunk]: ...
