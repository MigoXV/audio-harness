from __future__ import annotations

from collections.abc import AsyncIterator

import grpc

from audio_harness.configs.engine import EngineEndpoint
from audio_harness.inferencers.diarization.base import DiarizationInferencer
from audio_harness.inferencers.transports.grpc import GrpcTransport, raise_grpc_error
from audio_harness.protos.diarization import (
    ux_speaker_diarization_pb2 as diarization_pb2,
)
from audio_harness.protos.diarization import (
    ux_speaker_diarization_pb2_grpc as diarization_pb2_grpc,
)


def _duration_samples(value, sample_rate: int) -> int:
    seconds = value.seconds + value.nanos / 1_000_000_000
    return int(round(seconds * sample_rate))


class UxSpeakerDiarizationInferencer(DiarizationInferencer):
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
        self._stub = diarization_pb2_grpc.UxSpeakerDiarizationStub(
            self._transport.channel
        )

    async def ready(self) -> None:
        await self._transport.ready()

    async def probe(self) -> None:
        await self._transport.probe()

    async def close(self) -> None:
        await self._transport.close()

    async def stream_diarize(
        self,
        audio: bytes,
        sample_rate: int,
    ) -> AsyncIterator[tuple[DiarizationInferencer.Segment, ...]]:
        if not audio or len(audio) % 2:
            raise ValueError("diarization 输入必须是非空 PCM S16LE")
        if sample_rate <= 0:
            raise ValueError("diarization 采样率必须大于 0")
        request = diarization_pb2.DetectRequest(
            config=diarization_pb2.Config(
                encoding=diarization_pb2.Config.LINEAR16,
                sample_rate_hertz=sample_rate,
            ),
            audio=audio,
        )
        call = self._stub.StreamingDetect(
            request,
            timeout=self._transport.endpoint.request_timeout_seconds,
        )
        try:
            async for response in call:
                yield tuple(
                    DiarizationInferencer.Segment(
                        start_sample=_duration_samples(
                            result.start_time,
                            sample_rate,
                        ),
                        end_sample=_duration_samples(
                            result.end_time,
                            sample_rate,
                        ),
                        speaker=result.speaker,
                    )
                    for result in response.results
                )
        except grpc.aio.AioRpcError as exc:
            raise_grpc_error(exc, "Speaker Diarization")
