from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import grpc
import pytest

from audio_harness.serving.capacity import ServingCapacity
from audio_harness.configs import (
    AppConfig,
    CapacityConfig,
    WorkflowRuntimeConfig,
)
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.protos.asr import ux_speech_pb2 as pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as pb2_grpc
from audio_harness.serving.grpc import SpeechServicer
from audio_harness.sessions import AudioWorkflowSessionService
from audio_harness.workflows.settings import WorkflowSettingsSnapshot
from tests.graph_fakes import static_audio_workflow


class CompleteVad(VadInferencer):
    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadInferencer.Update]:
        total_bytes = 0
        async for chunk in audio_chunks:
            total_bytes += len(chunk)
        total_samples = total_bytes // 2
        yield VadInferencer.Update(
            boundaries=((0, total_samples),),
            processed_samples=total_samples,
        )

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        return True

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass


class EchoAsr(AsrInferencer):
    async def transcribe(
        self,
        audio: bytes,
        *,
        start_sample: int,
        end_sample: int,
        turn_completed: bool,
        options: AsrInferencer.Options,
    ) -> AsyncIterator[AsrInferencer.Result]:
        for text, final in (("interim", False), ("final", True)):
            yield AsrInferencer.Result(
                transcript=text,
                is_final=final,
                start_sample=start_sample,
                end_sample=end_sample,
                sample_rate=options.sample_rate,
                turn_completed=turn_completed,
                language_code=options.language_code,
            )

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass


class PendingSemanticVad(CompleteVad):
    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        return False


class StaticSettings:
    def __init__(self, config) -> None:
        self._snapshot = WorkflowSettingsSnapshot(
            config=config,
            grpc_audio_workflow_id="default-audio",
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
            },
        }
    )


async def start_server(
    *,
    semantic_vad: object | None = None,
) -> tuple[grpc.aio.Server, int]:
    server = grpc.aio.server()
    config = make_config()
    settings = StaticSettings(
        WorkflowRuntimeConfig(
            min_chunk_bytes=4,
            default_language_code=config.workflow.default_language_code,
        )
    )
    capacity = ServingCapacity(CapacityConfig())
    inferencer_pool, workflows, _workflow = static_audio_workflow(
        asr=EchoAsr(),
        vad=CompleteVad(),
        semantic_vad=semantic_vad,
    )
    sessions = AudioWorkflowSessionService(
        inferencer_pool=inferencer_pool,
        workflows=workflows,
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
    transcriptions = [result for result in results if result.alternative.transcript]
    assert [
        (result.alternative.transcript, result.is_final)
        for result in transcriptions
    ] == [("interim", False), ("final", True)]
    assert transcriptions[-1].alternative.words[0].end_time.seconds == 1
    assert all(result.alternative.turn_completed for result in transcriptions)


@pytest.mark.asyncio
async def test_streaming_recognize_reports_an_unfinished_semantic_turn() -> None:
    server, port = await start_server(semantic_vad=PendingSemanticVad())
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb2_grpc.UxSpeechStub(channel)

            async def requests():
                yield pb2.StreamingRecognizeRequest(
                    streaming_config=pb2.StreamingRecognitionConfig(
                        config=pb2.RecognitionConfig(
                            encoding=pb2.RecognitionConfig.LINEAR16,
                            sample_rate_hertz=4,
                        ),
                    )
                )
                yield pb2.StreamingRecognizeRequest(
                    audio_content=b"\x00\x00\x01\x00",
                )

            responses = [
                response async for response in stub.StreamingRecognize(requests())
            ]
    finally:
        await server.stop(0)

    result = responses[0].results[0]
    assert result.alternative.transcript == ""
    assert result.is_final is True
    assert result.alternative.turn_completed is False
    assert result.alternative.words[0].start_time.seconds == 0
    assert result.alternative.words[0].end_time.nanos == 500_000_000


@pytest.mark.asyncio
async def test_streaming_recognize_marks_a_completed_semantic_turn() -> None:
    server, port = await start_server(semantic_vad=CompleteVad())
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb2_grpc.UxSpeechStub(channel)

            async def requests():
                yield pb2.StreamingRecognizeRequest(
                    streaming_config=pb2.StreamingRecognitionConfig(
                        config=pb2.RecognitionConfig(
                            encoding=pb2.RecognitionConfig.LINEAR16,
                            sample_rate_hertz=4,
                        ),
                    )
                )
                yield pb2.StreamingRecognizeRequest(
                    audio_content=b"\x00\x00\x01\x00",
                )

            responses = [
                response async for response in stub.StreamingRecognize(requests())
            ]
    finally:
        await server.stop(0)

    assert all(
        result.alternative.turn_completed
        for response in responses
        for result in response.results
    )


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
