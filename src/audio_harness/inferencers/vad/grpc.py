from __future__ import annotations

from collections.abc import AsyncIterator

import grpc

from audio_harness.configs import EngineEndpoint
from audio_harness.inferencers.transports.grpc import (
    GrpcTransport,
    raise_grpc_error,
)
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.protos.vad import ux_vad_pb2 as vad_pb2
from audio_harness.protos.vad import ux_vad_pb2_grpc as vad_pb2_grpc


def duration_seconds(value) -> float:
    return value.seconds + value.nanos / 1_000_000_000


def sample_offset(value, sample_rate: int) -> int | None:
    seconds = duration_seconds(value)
    if seconds < 0:
        return None
    return int(round(seconds * sample_rate))


class UxVadInferencer(VadInferencer):
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
        self.stub = vad_pb2_grpc.UxVoiceActivityDetectorStub(
            self._transport.channel
        )

    async def ready(self) -> None:
        await self._transport.ready()

    async def probe(self) -> None:
        await self._transport.probe()

    async def close(self) -> None:
        await self._transport.close()

    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
        *,
        postprocessing: VadInferencer.Postprocessing | None = None,
    ) -> AsyncIterator[VadInferencer.Update]:
        async def requests() -> AsyncIterator[vad_pb2.StreamingDetectRequest]:
            config = vad_pb2.DetectionConfig(
                encoding=vad_pb2.DetectionConfig.LINEAR16,
                sample_rate_hertz=sample_rate,
            )
            if postprocessing is not None:
                config.postprocessing.CopyFrom(
                    vad_pb2.PostprocessingOptions(
                        processor=postprocessing["processor"],
                        version=postprocessing["version"],
                        parameters=dict(postprocessing["parameters"]),
                    )
                )
            yield vad_pb2.StreamingDetectRequest(
                config=config
            )
            async for chunk in audio_chunks:
                if chunk:
                    yield vad_pb2.StreamingDetectRequest(audio_content=chunk)

        call = self.stub.StreamingDetect(
            requests(),
            timeout=self._transport.endpoint.request_timeout_seconds,
        )
        try:
            async for response in call:
                processed = max(
                    0,
                    int(
                        round(
                            duration_seconds(response.processed_time)
                            * sample_rate
                        )
                    ),
                )
                boundaries = tuple(
                    (
                        sample_offset(
                            result.start_time,
                            sample_rate,
                        ),
                        sample_offset(
                            result.end_time,
                            sample_rate,
                        ),
                    )
                    for result in response.results
                )
                yield VadInferencer.Update(
                    boundaries=boundaries,
                    processed_samples=processed,
                )
        except grpc.aio.AioRpcError as exc:
            raise_grpc_error(exc, "VAD")

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        request = vad_pb2.DetectRequest(
            config=vad_pb2.DetectionConfig(
                encoding=vad_pb2.DetectionConfig.LINEAR16,
                sample_rate_hertz=sample_rate,
            ),
            audio=audio,
        )
        try:
            response = await self.stub.Detect(
                request,
                timeout=self._transport.endpoint.request_timeout_seconds,
            )
        except grpc.aio.AioRpcError as exc:
            raise_grpc_error(exc, "Semantic VAD")
        return any(
            duration_seconds(result.end_time) >= 0
            for result in response.results
        )
