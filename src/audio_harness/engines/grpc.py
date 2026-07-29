from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import grpc

from audio_harness.config import EngineEndpoint
from audio_harness.domain import (
    AudioSegment,
    EngineDeadlineError,
    EngineUnavailableError,
    RecognitionOptions,
    TranscriptionResult,
    VadBoundary,
    VadUpdate,
)
from audio_harness.protos.asr import ux_speech_pb2 as speech_pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as speech_pb2_grpc
from audio_harness.protos.vad import ux_vad_pb2 as vad_pb2
from audio_harness.protos.vad import ux_vad_pb2_grpc as vad_pb2_grpc

logger = logging.getLogger(__name__)


def _duration_seconds(value) -> float:
    return value.seconds + value.nanos / 1_000_000_000


def _sample_offset(value, sample_rate: int) -> int | None:
    seconds = _duration_seconds(value)
    if seconds < 0:
        return None
    return int(round(seconds * sample_rate))


def _raise_engine_error(error: grpc.aio.AioRpcError, engine_name: str) -> None:
    details = error.details() or error.code().name
    if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
        raise EngineDeadlineError(f"{engine_name} 请求超时: {details}") from error
    raise EngineUnavailableError(
        f"{engine_name} 请求失败({error.code().name}): {details}"
    ) from error


class _GrpcEngine:
    def __init__(self, endpoint: EngineEndpoint, *, max_message_bytes: int) -> None:
        self.endpoint = endpoint
        options = (
            ("grpc.max_send_message_length", max_message_bytes),
            ("grpc.max_receive_message_length", max_message_bytes),
        )
        self.channel = grpc.aio.insecure_channel(endpoint.target, options=options)

    async def ready(self) -> None:
        try:
            await asyncio.wait_for(
                self.channel.channel_ready(),
                timeout=self.endpoint.connect_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise EngineUnavailableError(
                f"连接引擎超时: {self.endpoint.target}"
            ) from exc

    async def close(self) -> None:
        await self.channel.close()


class GrpcVadEngine(_GrpcEngine):
    def __init__(self, endpoint: EngineEndpoint, *, max_message_bytes: int) -> None:
        super().__init__(endpoint, max_message_bytes=max_message_bytes)
        self.stub = vad_pb2_grpc.UxVoiceActivityDetectorStub(self.channel)

    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadUpdate]:
        async def requests() -> AsyncIterator[vad_pb2.StreamingDetectRequest]:
            yield vad_pb2.StreamingDetectRequest(
                config=vad_pb2.DetectionConfig(
                    encoding=vad_pb2.DetectionConfig.LINEAR16,
                    sample_rate_hertz=sample_rate,
                )
            )
            async for chunk in audio_chunks:
                if chunk:
                    yield vad_pb2.StreamingDetectRequest(audio_content=chunk)

        call = self.stub.StreamingDetect(
            requests(),
            timeout=self.endpoint.request_timeout_seconds,
        )
        try:
            async for response in call:
                processed = max(
                    0,
                    int(round(_duration_seconds(response.processed_time) * sample_rate)),
                )
                boundaries = tuple(
                    VadBoundary(
                        start_sample=_sample_offset(result.start_time, sample_rate),
                        end_sample=_sample_offset(result.end_time, sample_rate),
                    )
                    for result in response.results
                )
                yield VadUpdate(
                    boundaries=boundaries,
                    processed_samples=processed,
                )
        except grpc.aio.AioRpcError as exc:
            _raise_engine_error(exc, "VAD")

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
                timeout=self.endpoint.request_timeout_seconds,
            )
        except grpc.aio.AioRpcError as exc:
            _raise_engine_error(exc, "Semantic VAD")
        return any(_duration_seconds(result.end_time) >= 0 for result in response.results)


class GrpcAsrEngine(_GrpcEngine):
    def __init__(
        self,
        endpoint: EngineEndpoint,
        *,
        min_chunk_bytes: int,
        max_message_bytes: int,
    ) -> None:
        super().__init__(endpoint, max_message_bytes=max_message_bytes)
        self.min_chunk_bytes = min_chunk_bytes
        self.stub = speech_pb2_grpc.UxSpeechStub(self.channel)

    async def transcribe(
        self,
        segment: AudioSegment,
        options: RecognitionOptions,
    ) -> AsyncIterator[TranscriptionResult]:
        async def requests() -> AsyncIterator[speech_pb2.StreamingRecognizeRequest]:
            config = speech_pb2.RecognitionConfig(
                encoding=speech_pb2.RecognitionConfig.LINEAR16,
                sample_rate_hertz=options.sample_rate,
                language_code=options.language_code,
                enable_automatic_punctuation=options.enable_automatic_punctuation,
                hotwords=options.hotwords,
                hotword_bias=options.hotword_bias,
            )
            yield speech_pb2.StreamingRecognizeRequest(
                streaming_config=speech_pb2.StreamingRecognitionConfig(
                    config=config,
                    interim_results=True,
                )
            )
            for offset in range(0, len(segment.audio), self.min_chunk_bytes):
                chunk = segment.audio[offset : offset + self.min_chunk_bytes]
                if chunk:
                    yield speech_pb2.StreamingRecognizeRequest(audio_content=chunk)

        call = self.stub.StreamingRecognize(
            requests(),
            timeout=self.endpoint.request_timeout_seconds,
        )
        try:
            async for response in call:
                for result in response.results:
                    alternative = result.alternative
                    yield TranscriptionResult(
                        transcript=alternative.transcript,
                        is_final=result.is_final,
                        start_sample=segment.start_sample,
                        end_sample=segment.end_sample,
                        sample_rate=options.sample_rate,
                        turn_completed=segment.turn_completed,
                        language_code=options.language_code,
                        metadata=dict(alternative.metadata),
                    )
        except grpc.aio.AioRpcError as exc:
            _raise_engine_error(exc, "ASR")
