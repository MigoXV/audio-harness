from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from audio_harness.inferencers.embedding.base import (
    EngineFeatureInferencer,
    StreamingClusteringSession,
)


class DummyStreamingClusteringSession(StreamingClusteringSession):
    """仅维护向量数量，将所有输入归为说话人 0。"""

    def __init__(self) -> None:
        self._count = 0
        self._initialized = False
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("流式聚类会话已经关闭")

    async def start(
        self,
        vectors: tuple[bytes, ...],
        *,
        algorithm: str,
        parameters: Mapping[str, object],
    ) -> tuple[int, ...]:
        self._check_open()
        if self._initialized:
            raise RuntimeError("流式聚类会话已经初始化")
        if not vectors:
            raise ValueError("流式聚类首批 vectors 不能为空")
        self._count = len(vectors)
        self._initialized = True
        return (0,) * self._count

    async def add(self, vectors: tuple[bytes, ...]) -> tuple[int, ...]:
        self._check_open()
        if not self._initialized:
            raise RuntimeError("流式聚类会话尚未初始化")
        if not vectors:
            raise ValueError("流式聚类追加 vectors 不能为空")
        self._count += len(vectors)
        return (0,) * self._count

    async def close(self) -> None:
        self._closed = True


class DummyClusteringInferencer(EngineFeatureInferencer):
    """提供固定单位向量和单说话人聚类，不连接外部服务。"""

    minimum_clustering_vectors = 1

    async def ready(self) -> None:
        return None

    async def probe(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def embed(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> tuple[float, ...]:
        if not pcm_s16le or len(pcm_s16le) % 2:
            raise ValueError("embedding 输入必须是非空 PCM S16LE")
        if expected_dimension <= 0:
            raise ValueError("embedding 维度必须大于 0")
        return (1.0,) + (0.0,) * (expected_dimension - 1)

    async def embed_bytes(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> bytes:
        values = await self.embed(
            pcm_s16le,
            algorithm=algorithm,
            expected_dimension=expected_dimension,
            normalize=normalize,
        )
        return np.asarray(values, dtype="<f4").tobytes()

    def streaming_clustering(self) -> StreamingClusteringSession:
        return DummyStreamingClusteringSession()
