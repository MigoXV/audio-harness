from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping


class StreamingClusteringSession(ABC):
    """在一个远端流中维护持续追加的向量聚类状态。"""

    @abstractmethod
    async def start(
        self,
        vectors: tuple[bytes, ...],
        *,
        algorithm: str,
        parameters: Mapping[str, object],
    ) -> tuple[int, ...]: ...

    @abstractmethod
    async def add(self, vectors: tuple[bytes, ...]) -> tuple[int, ...]: ...

    @abstractmethod
    async def close(self) -> None: ...


class EngineFeatureInferencer(ABC):
    """统一提供音频向量提取与流式聚类能力。"""

    @abstractmethod
    async def ready(self) -> None: ...

    @abstractmethod
    async def probe(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def embed(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> tuple[float, ...]: ...

    @abstractmethod
    async def embed_bytes(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> bytes: ...

    @abstractmethod
    def streaming_clustering(self) -> StreamingClusteringSession: ...
