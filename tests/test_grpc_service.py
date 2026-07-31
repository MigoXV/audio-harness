from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import grpc
import pytest

from audio_harness.capacity import ServingCapacity
from audio_harness.config import AppConfig, CapacityConfig
from audio_harness.domain import (
    AudioSegment,
    EngineSet,
    RecognitionOptions,
    TranscriptionResult,
    VadBoundary,
    VadUpdate,
)
from audio_harness.protos.asr import ux_speech_pb2 as pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as pb2_grpc
from audio_harness.serving.grpc import SpeechServicer
from audio_harness.sessions import TranscriptionSessionService
from audio_harness.workflow_settings import WorkflowSettingsSnapshot


class CompleteVad:
    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadUpdate]:
        total_bytes = 0
        async for chunk in audio_chunks:
            total_bytes += len(chunk)
        total_samples = total_bytes // 2
        yield VadUpdate(
            boundaries=(VadBoundary(0, total_samples),),
            processed_samples=total_samples,
        )

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        return True


class EchoAsr:
    async def transcribe(
        self,
        segment: AudioSegment,
        options: RecognitionOptions,
    ) -> AsyncIterator[TranscriptionResult]:
        for text, final in (("interim", False), ("final", True)):
            yield TranscriptionResult(
                transcript=text,
                is_final=final,
                start_sample=segment.start_sample,
                end_sample=segment.end_sample,
                sample_rate=options.sample_rate,
                turn_completed=True,
                language_code=options.language_code,
            )


class StaticProvider:
    @asynccontextmanager
    async def lease(self):
        yield EngineSet(
            asr=EchoAsr(),
            vad=CompleteVad(),
            semantic_vad=None,
        )


class StaticSettings:
    def __init__(self, config) -> None:
        self._snapshot = WorkflowSettingsSnapshot(
            config=config,
            revision=1,
            updated_at=datetime.now(timezone.utc),
        )

    def current(self) -> WorkflowSettingsSnapshot:
        return self._snapshot


def make_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "workflow": {
                "min_chunk_bytes": 4,
                "min_segment_seconds": 0,
            },
        }
    )


async def start_server() -> tuple[grpc.aio.Server, int]:
    server = grpc.aio.server()
    config = make_config()
    settings = StaticSettings(config.workflow)
    capacity = ServingCapacity(CapacityConfig())
    sessions = TranscriptionSessionService(
        engine_provider=StaticProvider(),
        workflow_settings=settings,
        session_limiter=capacity.grpc_sessions,
        asr_limiter=capacity.asr,
    )
    pb2_grpc.add_UxSpeechServicer_to_server(
        SpeechServicer(
            config=config,
            session_service=sessions,
            workflow_settings=settings,
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, port


@pytest.mark.asyncio
async def test_streaming_recognize_always_passes_interim_results() -> None:
    server, port = await start_server()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb2_grpc.UxSpeechStub(channel)

            async def requests():
                yield pb2.StreamingRecognizeRequest(
                    streaming_config=pb2.StreamingRecognitionConfig(
                        config=pb2.RecognitionConfig(
                            encoding=pb2.RecognitionConfig.LINEAR16,
                            sample_rate_hertz=4,
                            language_code="zh-CN",
                        ),
                        interim_results=False,
                    )
                )
                yield pb2.StreamingRecognizeRequest(audio_content=b"\x00\x00\x01\x00")
                yield pb2.StreamingRecognizeRequest(audio_content=b"\x02\x00\x03\x00")

            responses = [response async for response in stub.StreamingRecognize(requests())]
    finally:
        await server.stop(0)

    results = [response.results[0] for response in responses]
    assert [(result.alternative.transcript, result.is_final) for result in results] == [
        ("interim", False),
        ("final", True),
    ]
    assert results[-1].alternative.turn_completed is True
    assert results[-1].alternative.words[0].end_time.seconds == 1


@pytest.mark.asyncio
async def test_rejects_audio_before_streaming_config() -> None:
    server, port = await start_server()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb2_grpc.UxSpeechStub(channel)

            async def requests():
                yield pb2.StreamingRecognizeRequest(audio_content=b"\x00\x00")

            with pytest.raises(grpc.aio.AioRpcError) as error:
                await stub.StreamingRecognize(requests()).read()
            assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await server.stop(0)
