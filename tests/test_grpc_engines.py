from __future__ import annotations

from collections.abc import AsyncIterator

import grpc
import pytest
from google.protobuf import duration_pb2

from audio_harness.config import EngineEndpoint
from audio_harness.domain import AudioSegment, RecognitionOptions
from audio_harness.engines.grpc import GrpcAsrEngine, GrpcVadEngine
from audio_harness.protos.asr import ux_speech_pb2 as speech_pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as speech_pb2_grpc
from audio_harness.protos.vad import ux_vad_pb2 as vad_pb2
from audio_harness.protos.vad import ux_vad_pb2_grpc as vad_pb2_grpc


def duration(seconds: float) -> duration_pb2.Duration:
    return duration_pb2.Duration(
        seconds=int(seconds),
        nanos=int((seconds - int(seconds)) * 1_000_000_000),
    )


class FakeVadServicer(vad_pb2_grpc.UxVoiceActivityDetectorServicer):
    def __init__(self) -> None:
        self.streaming_config: vad_pb2.DetectionConfig | None = None
        self.streaming_audio = bytearray()
        self.detect_audio = b""

    async def StreamingDetect(self, request_iterator, context):
        async for request in request_iterator:
            if request.HasField("config"):
                self.streaming_config = request.config
            elif request.HasField("audio_content"):
                self.streaming_audio.extend(request.audio_content)
        yield vad_pb2.StreamingDetectResponse(
            results=[
                vad_pb2.DetectionResult(
                    start_time=duration(0),
                    end_time=duration(1),
                )
            ],
            processed_time=duration(1),
        )

    async def Detect(self, request, context):
        self.detect_audio = request.audio
        return vad_pb2.DetectResponse(
            results=[
                vad_pb2.DetectionResult(
                    start_time=duration(0),
                    end_time=duration(1),
                )
            ]
        )


class FakeSpeechServicer(speech_pb2_grpc.UxSpeechServicer):
    def __init__(self) -> None:
        self.config: speech_pb2.StreamingRecognitionConfig | None = None
        self.audio = bytearray()

    async def StreamingRecognize(self, request_iterator, context):
        async for request in request_iterator:
            if request.HasField("streaming_config"):
                self.config = request.streaming_config
            elif request.HasField("audio_content"):
                self.audio.extend(request.audio_content)
        for text, final in (("interim", False), ("final", True)):
            yield speech_pb2.StreamingRecognizeResponse(
                results=[
                    speech_pb2.StreamingRecognitionResult(
                        alternative=speech_pb2.SpeechRecognitionAlternative(
                            transcript=text,
                            metadata={"engine": "fake"},
                        ),
                        is_final=final,
                    )
                ]
            )


async def audio_chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_grpc_vad_engine_uses_async_stream_and_unary_turn_detection() -> None:
    fake = FakeVadServicer()
    server = grpc.aio.server()
    vad_pb2_grpc.add_UxVoiceActivityDetectorServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    engine = GrpcVadEngine(
        EngineEndpoint(target=f"127.0.0.1:{port}"),
        max_message_bytes=1024,
    )
    try:
        await engine.ready()
        updates = [
            update
            async for update in engine.stream_detect(
                audio_chunks(b"\x00\x00", b"\x01\x00"),
                sample_rate=4,
            )
        ]
        completed = await engine.detect_turn(b"\x00\x00\x01\x00", sample_rate=4)
    finally:
        await engine.close()
        await server.stop(0)

    assert len(updates) == 1
    assert updates[0].processed_samples == 4
    assert updates[0].boundaries[0].start_sample == 0
    assert updates[0].boundaries[0].end_sample == 4
    assert completed is True
    assert fake.streaming_config is not None
    assert fake.streaming_config.sample_rate_hertz == 4
    assert bytes(fake.streaming_audio) == b"\x00\x00\x01\x00"


@pytest.mark.asyncio
async def test_grpc_asr_engine_always_requests_and_returns_interim_results() -> None:
    fake = FakeSpeechServicer()
    server = grpc.aio.server()
    speech_pb2_grpc.add_UxSpeechServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    engine = GrpcAsrEngine(
        EngineEndpoint(target=f"127.0.0.1:{port}"),
        min_chunk_bytes=4,
        max_message_bytes=1024,
    )
    segment = AudioSegment(
        audio=b"\x00\x00\x01\x00\x02\x00\x03\x00",
        start_sample=8,
        end_sample=12,
    )
    options = RecognitionOptions(
        sample_rate=4,
        language_code="zh-CN",
        hotwords=("Audio Harness",),
        hotword_bias=2.0,
    )
    try:
        await engine.ready()
        results = [
            result async for result in engine.transcribe(segment, options)
        ]
    finally:
        await engine.close()
        await server.stop(0)

    assert [(item.transcript, item.is_final) for item in results] == [
        ("interim", False),
        ("final", True),
    ]
    assert results[-1].metadata == {"engine": "fake"}
    assert (results[-1].start_sample, results[-1].end_sample) == (8, 12)
    assert fake.config is not None
    assert fake.config.interim_results is True
    assert fake.config.config.hotwords == ["Audio Harness"]
    assert bytes(fake.audio) == segment.audio
