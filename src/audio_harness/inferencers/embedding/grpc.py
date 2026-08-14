from __future__ import annotations

import asyncio

import grpc
import numpy as np

from audio_harness.configs import EngineEndpoint
from audio_harness.inferencers.transports.grpc import GrpcTransport, raise_grpc_error
from audio_harness.protos.embedding import engine_pb2, engine_pb2_grpc


class AudioEmbeddingInferencer:
    """将临时 Feature 协议隔离为稳定的 float32 音频向量接口。"""

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
