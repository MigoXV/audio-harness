from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from types import MappingProxyType
from typing import NamedTuple


_EMPTY_METADATA: Mapping[str, str] = MappingProxyType({})


class AsrInferencer(ABC):
    class Options(NamedTuple):
        sample_rate: int
        language_code: str
        enable_automatic_punctuation: bool = True
        hotwords: tuple[str, ...] = ()
        hotword_bias: float = 0.0

    class Result(NamedTuple):
        transcript: str
        is_final: bool
        start_sample: int
        end_sample: int
        sample_rate: int
        turn_completed: bool
        language_code: str | None = None
        metadata: Mapping[str, str] = _EMPTY_METADATA
        physical_segment_ids: tuple[str, ...] = ()

    @abstractmethod
    async def ready(self) -> None: ...

    @abstractmethod
    async def probe(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def transcribe(
        self,
        audio: bytes,
        *,
        start_sample: int,
        end_sample: int,
        turn_completed: bool,
        options: Options,
    ) -> AsyncIterator[Result]: ...
