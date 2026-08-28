from __future__ import annotations

import asyncio
from collections.abc import Mapping

import grpc
import numpy as np

from audio_harness.configs import EngineEndpoint
from audio_harness.errors import EngineDeadlineError, EngineUnavailableError
from audio_harness.inferencers.embedding.base import (
    EngineFeatureInferencer,
    StreamingClusteringSession,
)
from audio_harness.inferencers.transports.grpc import GrpcTransport, raise_grpc_error
from audio_harness.protos.embedding import engine_pb2, engine_pb2_grpc


class GrpcStreamingClusteringSession(StreamingClusteringSession):
    def __init__(
        self,
        stub: engine_pb2_grpc.EngineStub,
        *,
        timeout: float | None,
    ) -> None:
        self._call = stub.StreamingClustering()
        self._timeout = timeout
        self._sequence = 0
        self._initialized = False
        self._closed = False

    async def _await(self, value, operation: str):
        try:
            return (
                await asyncio.wait_for(value, timeout=self._timeout)
                if self._timeout is not None
                else await value
            )
        except asyncio.TimeoutError as exc:
            self._call.cancel()
            raise EngineDeadlineError(f"流式聚类{operation}超时") from exc
        except grpc.aio.AioRpcError as exc:
            raise_grpc_error(exc, "流式聚类")
            raise AssertionError("unreachable")

    async def _read(self, expected_sequence: int) -> tuple[int, ...]:
        response = await self._await(self._call.read(), "响应")
        if response is grpc.aio.EOF:
            code = await self._call.code()
            details = await self._call.details()
            raise EngineUnavailableError(
                f"流式聚类提前结束({code.name}): {details or code.name}"
            )
        if int(response.sequence) != expected_sequence:
            raise ValueError(
                "流式聚类响应 sequence 不匹配: "
                f"expected={expected_sequence}, actual={response.sequence}"
            )
        return tuple(int(label) for label in response.labels)

    async def start(
        self,
        vectors: tuple[bytes, ...],
        *,
        algorithm: str,
        parameters: Mapping[str, object],
    ) -> tuple[int, ...]:
        if self._initialized:
            raise RuntimeError("流式聚类会话已经初始化")
        if not vectors:
            raise ValueError("流式聚类首批 vectors 不能为空")
        config = engine_pb2.ClusteringRequest(
            vectors=vectors,
            algorithm=algorithm,
        )
        config.parameters.update(dict(parameters))
        await self._await(
            self._call.write(
                engine_pb2.StreamingClusteringRequest(config=config)
            ),
            "写入",
        )
        self._initialized = True
        return await self._read(0)

    async def add(self, vectors: tuple[bytes, ...]) -> tuple[int, ...]:
        if not self._initialized:
            raise RuntimeError("流式聚类会话尚未初始化")
        if not vectors:
            raise ValueError("流式聚类追加 vectors 不能为空")
        self._sequence += 1
        await self._await(
            self._call.write(
                engine_pb2.StreamingClusteringRequest(
                    batch=engine_pb2.ClusteringVectorBatch(
                        sequence=self._sequence,
                        vectors=vectors,
                    )
                )
            ),
            "写入",
        )
        return await self._read(self._sequence)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._call.done():
            try:
                await self._call.done_writing()
            except (grpc.aio.AioRpcError, asyncio.InvalidStateError):
                self._call.cancel()


class GrpcEngineFeatureInferencer(EngineFeatureInferencer):
    """Engine Feature gRPC 客户端。"""

    def __init__(self, endpoint: EngineEndpoint, *, max_message_bytes: int) -> None:
        self._transport = GrpcTransport(endpoint, max_message_bytes=max_message_bytes)
        self._stub = engine_pb2_grpc.EngineStub(self._transport.channel)

    async def ready(self) -> None:
        await self._transport.ready()

    async def probe(self) -> None:
        await self._transport.probe()

    async def close(self) -> None:
        await self._transport.close()

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
        request = engine_pb2.FeatRequest(vadAlg="", sidAlg=algorithm, pcm=pcm_s16le)
        try:
            call = self._stub.Feature(request)
            timeout = self._transport.endpoint.request_timeout_seconds
            reply = await asyncio.wait_for(call, timeout=timeout) if timeout else await call
        except asyncio.TimeoutError as exc:
            raise RuntimeError("音频向量请求超时") from exc
        except grpc.aio.AioRpcError as exc:
            raise_grpc_error(exc, "音频向量")
            raise AssertionError("unreachable")
        if expected_dimension <= 0 or len(reply.feat) != expected_dimension * 4:
            raise ValueError(
                f"embedding 维度不符: expected={expected_dimension}, bytes={len(reply.feat)}"
            )
        values = np.frombuffer(reply.feat, dtype="<f4").astype(np.float32, copy=True)
        if not np.isfinite(values).all():
            raise ValueError("embedding 包含 NaN 或 Infinity")
        if normalize:
            norm = float(np.linalg.norm(values))
            if norm <= 0:
                raise ValueError("embedding 范数为 0，无法归一化")
            values /= norm
        return tuple(float(value) for value in values)

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
        return GrpcStreamingClusteringSession(
            self._stub,
            timeout=self._transport.endpoint.request_timeout_seconds,
        )
