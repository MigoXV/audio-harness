from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import NamedTuple


class StreamingDiarizationInferencer(ABC):
    """单个调用拥有独立的说话人身份和从零开始的采样时间轴。"""

    class Boundary(NamedTuple):
        segment_id: int
        speaker: int
        start_sample: int | None
        end_sample: int | None

    class Update(NamedTuple):
        boundaries: tuple[StreamingDiarizationInferencer.Boundary, ...]
        processed_samples: int

    @abstractmethod
    async def ready(self) -> None: ...

    @abstractmethod
    async def probe(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def stream_diarize(
        self,
        audio: AsyncIterator[bytes],
        sample_rate: int,
        *,
        input_buffer_latency_ms: int,
    ) -> AsyncIterator[Update]: ...
