from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import grpc
import pytest
from google.protobuf import duration_pb2

from audio_harness.config import EngineEndpoint
from audio_harness.domain import (
    AudioSegment,
    PresetVoice,
    RecognitionOptions,
    VadPostprocessingOptions,
)
from audio_harness.inferencers.grpc import (
    GrpcAsrInferencer,
    GrpcDuplexTtsInferencer,
    GrpcVadInferencer,
)
from audio_harness.protos.asr import ux_speech_pb2 as speech_pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as speech_pb2_grpc
from audio_harness.protos.tts import tts_pb2, tts_pb2_grpc
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
        self.audio_message_count = 0

    async def StreamingRecognize(self, request_iterator, context):
        async for request in request_iterator:
            if request.HasField("streaming_config"):
                self.config = request.streaming_config
            elif request.HasField("audio_content"):
                self.audio_message_count += 1
                if self.audio_message_count > 1:
                    await context.abort(
                        grpc.StatusCode.INVALID_ARGUMENT,
                        "Only a single audio_content message is supported.",
                    )
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


class DelayedTtsServicer(tts_pb2_grpc.TtsServiceServicer):
    async def DuplexSynthesize(self, request_iterator, _context):
        async for _request in request_iterator:
            pass
        await asyncio.sleep(0.1)
        yield tts_pb2.AudioChunk(
            pcm_s16le=b"\0\0",
            sample_rate=24_000,
            chunk_index=0,
        )


async def audio_chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_grpc_vad_inferencer_uses_async_stream_and_unary_turn_detection() -> None:
    fake = FakeVadServicer()
    server = grpc.aio.server()
    vad_pb2_grpc.add_UxVoiceActivityDetectorServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    inferencer = GrpcVadInferencer(
        EngineEndpoint(target=f"127.0.0.1:{port}"),
        max_message_bytes=1024,
    )
    try:
        await inferencer.ready()
        updates = [
            update
            async for update in inferencer.stream_detect(
                audio_chunks(b"\x00\x00", b"\x01\x00"),
                sample_rate=4,
            )
        ]
        completed = await inferencer.detect_turn(
            b"\x00\x00\x01\x00",
            sample_rate=4,
        )
    finally:
        await inferencer.close()
        await server.stop(0)

    assert len(updates) == 1
    assert updates[0].processed_samples == 4
    assert updates[0].boundaries[0].start_sample == 0
    assert updates[0].boundaries[0].end_sample == 4
    assert completed is True
    assert fake.streaming_config is not None
    assert fake.streaming_config.sample_rate_hertz == 4
    assert not fake.streaming_config.HasField("postprocessing")
    assert bytes(fake.streaming_audio) == b"\x00\x00\x01\x00"


@pytest.mark.asyncio
async def test_grpc_vad_inferencer_sends_postprocessing_only_in_stream_config() -> None:
    fake = FakeVadServicer()
    server = grpc.aio.server()
    vad_pb2_grpc.add_UxVoiceActivityDetectorServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    inferencer = GrpcVadInferencer(
        EngineEndpoint(target=f"127.0.0.1:{port}"),
        max_message_bytes=1024,
    )
    try:
        updates = [
            update
            async for update in inferencer.stream_detect(
                audio_chunks(b"\x00\x00"),
                sample_rate=16_000,
                postprocessing=VadPostprocessingOptions(
                    processor="firered.stream_vad",
                    version=1,
                    parameters={
                        "speech_threshold": 0.7,
                        "min_silence_frame": 12,
                    },
                ),
            )
        ]
    finally:
        await inferencer.close()
        await server.stop(0)

    assert len(updates) == 1
    assert fake.streaming_config is not None
    assert fake.streaming_config.HasField("postprocessing")
    assert fake.streaming_config.postprocessing.processor == "firered.stream_vad"
    assert fake.streaming_config.postprocessing.version == 1
    assert dict(fake.streaming_config.postprocessing.parameters) == {
        "speech_threshold": 0.7,
        "min_silence_frame": 12.0,
    }


@pytest.mark.asyncio
async def test_grpc_asr_inferencer_always_requests_and_returns_interim_results() -> None:
    fake = FakeSpeechServicer()
    server = grpc.aio.server()
    speech_pb2_grpc.add_UxSpeechServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    inferencer = GrpcAsrInferencer(
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
        await inferencer.ready()
        results = [
            result
            async for result in inferencer.transcribe(segment, options)
        ]
    finally:
        await inferencer.close()
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
    assert fake.audio_message_count == 1
    assert bytes(fake.audio) == segment.audio


@pytest.mark.asyncio
async def test_grpc_tts_stream_is_not_cut_off_by_request_timeout() -> None:
    server = grpc.aio.server()
    tts_pb2_grpc.add_TtsServiceServicer_to_server(
        DelayedTtsServicer(), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    inferencer = GrpcDuplexTtsInferencer(
        EngineEndpoint(
            target=f"127.0.0.1:{port}",
            request_timeout_seconds=0.01,
        ),
        max_message_bytes=1024,
    )

    async def text_chunks() -> AsyncIterator[str]:
        yield "测试"

    try:
        chunks = [
            chunk
            async for chunk in inferencer.duplex_synthesize(
                text_chunks(),
                voice=PresetVoice("anbi"),
                language="Chinese",
                decoder_chunk_size=0,
            )
        ]
    finally:
        await inferencer.close()
        await server.stop(0)

    assert [chunk.pcm_s16le for chunk in chunks] == [b"\0\0"]
