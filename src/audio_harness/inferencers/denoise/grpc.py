from __future__ import annotations

from collections.abc import AsyncIterator

import grpc

from audio_harness.configs.engine import EngineEndpoint
from audio_harness.inferencers.denoise.base import DenoiseInferencer
from audio_harness.inferencers.transports.grpc import GrpcTransport, raise_grpc_error
from audio_harness.protos.denoise import ux_denoise_pb2 as denoise_pb2
from audio_harness.protos.denoise import ux_denoise_pb2_grpc as denoise_pb2_grpc


class UxDenoiseInferencer(DenoiseInferencer):
    def __init__(
        self,
        endpoint: EngineEndpoint,
        *,
        max_message_bytes: int,
    ) -> None:
        self._transport = GrpcTransport(
            endpoint,
            max_message_bytes=max_message_bytes,
        )
        self.stub = denoise_pb2_grpc.UxDenoiseStub(self._transport.channel)

    async def ready(self) -> None:
        await self._transport.ready()

    async def probe(self) -> None:
        await self._transport.probe()

    async def close(self) -> None:
        await self._transport.close()

    async def stream_synchronized(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[DenoiseInferencer.SynchronizedChunk]:
        if sample_rate <= 0:
            raise ValueError("降噪采样率必须大于 0")

        async def requests() -> AsyncIterator[denoise_pb2.StreamingDenoiseRequest]:
            yield denoise_pb2.StreamingDenoiseRequest(
                config=denoise_pb2.DenoiseConfig(
                    audio_in_config=denoise_pb2.AudioInConfig(
                        encoding=denoise_pb2.LINEAR16,
                        sample_rate_hertz=sample_rate,
                    ),
                    audio_out_config=denoise_pb2.AudioOutConfig(
                        encoding=denoise_pb2.LINEAR16,
                        sample_rate_hertz=sample_rate,
                    ),
                )
            )
            async for chunk in audio_chunks:
                if not chunk or len(chunk) % 2:
                    raise ValueError("降噪输入必须是非空 PCM S16LE")
                yield denoise_pb2.StreamingDenoiseRequest(audio_in=chunk)

        call = self.stub.SynchronizedStreamingDenoise(
            requests(),
            timeout=self._transport.endpoint.request_timeout_seconds,
        )
        try:
            async for response in call:
                original = response.audio_in
                denoised = response.audio_out
                if not original or len(original) % 2:
                    raise ValueError("同步降噪返回了非法原始 PCM")
                if len(original) != len(denoised):
                    raise ValueError("同步降噪返回的原音与降噪音频长度不一致")
                yield DenoiseInferencer.SynchronizedChunk(original, denoised)
        except grpc.aio.AioRpcError as exc:
            raise_grpc_error(exc, "Denoise")
