from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import grpc
import numpy as np
import pytest
from google.protobuf import duration_pb2

from audio_harness.configs import EngineEndpoint
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.asr.grpc import UxSpeechAsrInferencer
from audio_harness.inferencers.tts.grpc import QwenDuplexTtsInferencer
from audio_harness.inferencers.tts.base import TtsInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.inferencers.vad.grpc import UxVadInferencer
from audio_harness.inferencers.diarization.grpc import (
    UxSpeakerDiarizationInferencer,
)
from audio_harness.inferencers.embedding import AudioEmbeddingInferencer
from audio_harness.protos.embedding import engine_pb2, engine_pb2_grpc
from audio_harness.protos.asr import ux_speech_pb2 as speech_pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as speech_pb2_grpc
from audio_harness.protos.tts import tts_pb2, tts_pb2_grpc
from audio_harness.protos.vad import ux_vad_pb2 as vad_pb2
from audio_harness.protos.vad import ux_vad_pb2_grpc as vad_pb2_grpc
from audio_harness.protos.diarization import (
    ux_speaker_diarization_pb2 as diarization_pb2,
)
from audio_harness.protos.diarization import (
    ux_speaker_diarization_pb2_grpc as diarization_pb2_grpc,
)


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


class FakeDiarizationServicer(
    diarization_pb2_grpc.UxSpeakerDiarizationServicer
):
    def __init__(self) -> None:
        self.request: diarization_pb2.DetectRequest | None = None
        self.unary_called = False

    async def StreamingDetect(self, request, _context):
        self.request = request
        yield diarization_pb2.DetectResponse(
            results=[
                diarization_pb2.DetectionResult(
                    start_time=duration(0.25),
                    end_time=duration(0.75),
                    speaker=2,
                )
            ]
        )
        yield diarization_pb2.DetectResponse(
            results=[
                diarization_pb2.DetectionResult(
                    start_time=duration(0.75),
                    end_time=duration(1),
                    speaker=1,
                )
            ]
        )

    async def Detect(self, _request, _context):
        self.unary_called = True
        return diarization_pb2.DetectResponse()


class FakeEmbeddingServicer(engine_pb2_grpc.EngineServicer):
    def __init__(self) -> None:
        self.requests: list[engine_pb2.StreamingClusteringRequest] = []

    async def Feature(self, _request, _context):
        return engine_pb2.FeatReply(
            feat=np.asarray([3.0, 4.0], dtype="<f4").tobytes()
        )

    async def StreamingClustering(self, request_iterator, _context):
        count = 0
        async for request in request_iterator:
            self.requests.append(request)
            if request.HasField("config"):
                count = len(request.config.vectors)
                sequence = 0
            else:
                count += len(request.batch.vectors)
                sequence = request.batch.sequence
            yield engine_pb2.StreamingClusteringReply(
                sequence=sequence,
                labels=list(range(count)),
            )


async def audio_chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_embedding_streaming_clustering_returns_full_revisions() -> None:
    fake = FakeEmbeddingServicer()
    server = grpc.aio.server()
    engine_pb2_grpc.add_EngineServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    inferencer = AudioEmbeddingInferencer(
        EngineEndpoint(target=f"127.0.0.1:{port}"),
        max_message_bytes=4096,
    )
    session = inferencer.streaming_clustering()
    try:
        vector = await inferencer.embed_bytes(
            b"\x00\x00",
            algorithm="eres2netv2",
            expected_dimension=2,
        )
        first = await session.start(
            (vector, vector),
            algorithm="hdbscan",
            parameters={"min_cluster_size": 2, "allow_single_cluster": True},
        )
        second = await session.add((vector,))
    finally:
        await session.close()
        await inferencer.close()
        await server.stop(0)

    assert np.frombuffer(vector, dtype="<f4").tolist() == pytest.approx([0.6, 0.8])
    assert first == (0, 1)
    assert second == (0, 1, 2)
    assert fake.requests[0].config.algorithm == "hdbscan"
    assert fake.requests[0].config.parameters["min_cluster_size"] == 2
    assert fake.requests[1].batch.sequence == 1


@pytest.mark.asyncio
async def test_grpc_vad_inferencer_uses_async_stream_and_unary_turn_detection() -> None:
    fake = FakeVadServicer()
    server = grpc.aio.server()
    vad_pb2_grpc.add_UxVoiceActivityDetectorServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    inferencer = UxVadInferencer(
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
    assert updates[0].boundaries[0] == (0, 4)
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
    inferencer = UxVadInferencer(
        EngineEndpoint(target=f"127.0.0.1:{port}"),
        max_message_bytes=1024,
    )
    try:
        updates = [
            update
            async for update in inferencer.stream_detect(
                audio_chunks(b"\x00\x00"),
                sample_rate=16_000,
                postprocessing=VadInferencer.Postprocessing(
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
async def test_grpc_diarization_inferencer_uses_server_streaming_only(
    tmp_path,
) -> None:
    fake = FakeDiarizationServicer()
    server = grpc.aio.server()
    diarization_pb2_grpc.add_UxSpeakerDiarizationServicer_to_server(
        fake,
        server,
    )
    target = f"unix:{tmp_path / 'diarization.sock'}"
    server.add_insecure_port(target)
    await server.start()
    inferencer = UxSpeakerDiarizationInferencer(
        EngineEndpoint(target=target),
        max_message_bytes=1024,
    )
    try:
        updates = [
            update
            async for update in inferencer.stream_diarize(
                b"\x00\x00" * 4,
                sample_rate=4,
            )
        ]
    finally:
        await inferencer.close()
        await server.stop(0)

    assert updates == [((1, 3, 2),), ((3, 4, 1),)]
    assert fake.request is not None
    assert fake.request.config.encoding == diarization_pb2.Config.LINEAR16
    assert fake.request.config.sample_rate_hertz == 4
    assert fake.request.audio == b"\x00\x00" * 4
    assert fake.unary_called is False


@pytest.mark.asyncio
async def test_grpc_asr_inferencer_always_requests_and_returns_interim_results() -> None:
    fake = FakeSpeechServicer()
    server = grpc.aio.server()
    speech_pb2_grpc.add_UxSpeechServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    inferencer = UxSpeechAsrInferencer(
        EngineEndpoint(target=f"127.0.0.1:{port}"),
        max_message_bytes=1024,
    )
    audio = b"\x00\x00\x01\x00\x02\x00\x03\x00"
    options = AsrInferencer.Options(
        sample_rate=4,
        language_code="zh-CN",
        hotwords=("Audio Harness",),
        hotword_bias=2.0,
    )
    try:
        await inferencer.ready()
        results = [
            result
            async for result in inferencer.transcribe(
                audio,
                start_sample=8,
                end_sample=12,
                turn_completed=True,
                options=options,
            )
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
    assert bytes(fake.audio) == audio


@pytest.mark.asyncio
async def test_grpc_tts_stream_is_not_cut_off_by_request_timeout() -> None:
    server = grpc.aio.server()
    tts_pb2_grpc.add_TtsServiceServicer_to_server(
        DelayedTtsServicer(), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    inferencer = QwenDuplexTtsInferencer(
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
                voice="anbi",
                language="Chinese",
                decoder_chunk_size=0,
            )
        ]
    finally:
        await inferencer.close()
        await server.stop(0)

    assert [chunk.pcm_s16le for chunk in chunks] == [b"\0\0"]
