from __future__ import annotations

import asyncio
from contextlib import aclosing, asynccontextmanager

import grpc
import pytest
from google.protobuf.duration_pb2 import Duration

from audio_harness.configs.engine import EngineEndpoint
from audio_harness.errors import EngineUnavailableError
from audio_harness.inferencers.diarization.streaming_grpc import UxStreamingDiarizationInferencer
from audio_harness.protos.diarization import ux_speaker_diarization_pb2 as pb
from audio_harness.protos.diarization import ux_speaker_diarization_pb2_grpc as rpc


def duration(samples):
    if samples is None:
        return Duration(seconds=-1)
    return Duration(seconds=samples // 16000, nanos=(samples % 16000) * 62500)


class Service(rpc.UxSpeakerDiarizationServicer):
    def __init__(self, *, abort=False, malformed=False):
        self.configs = []
        self.audios = []
        self.finished = asyncio.Event()
        self.half_closed = False
        self.abort = abort
        self.malformed = malformed

    async def DuplexStreamingDetect(self, requests, context):
        total = 0
        try:
            config = await anext(requests)
            self.configs.append(config.config)
            async for request in requests:
                assert request.WhichOneof("streaming_request") == "audio_content"
                if self.abort:
                    await context.abort(grpc.StatusCode.UNAVAILABLE, "offline")
                first = total == 0
                self.audios.append(request.audio_content)
                total += len(request.audio_content) // 2
                yield pb.DuplexStreamingDetectResponse(
                    process_time=Duration(seconds=-2) if self.malformed else duration(total),
                    results=[pb.DuplexDetectionResult(
                        segment_id=7, speaker=2,
                        start_time=duration(0 if first else None), end_time=duration(None),
                    )],
                )
            self.half_closed = True
            yield pb.DuplexStreamingDetectResponse(
                process_time=duration(total), results=[pb.DuplexDetectionResult(
                    segment_id=7, speaker=2, start_time=duration(None), end_time=duration(total),
                )],
            )
        finally:
            self.finished.set()


@asynccontextmanager
async def running(service):
    server = grpc.aio.server()
    rpc.add_UxSpeakerDiarizationServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    client = UxStreamingDiarizationInferencer(
        EngineEndpoint(target=f"127.0.0.1:{port}", connect_timeout_seconds=2),
        max_message_bytes=1024 * 1024,
    )
    try:
        yield client
    finally:
        await client.close()
        await server.stop(None)


@pytest.mark.asyncio
async def test_duplex_receives_before_half_close_and_flushes():
    release = asyncio.Event()
    service = Service()

    async def audio():
        yield bytes(634)
        await release.wait()
        yield bytes(200)

    async with running(service) as client:
        async with aclosing(client.stream_diarize(audio(), 16000, input_buffer_latency_ms=640)) as stream:
            first = await asyncio.wait_for(anext(stream), 2)
            assert first.processed_samples == 317
            assert first.boundaries[0] == (7, 2, 0, None)
            assert not service.half_closed
            release.set()
            rest = [update async for update in stream]
        assert rest[0].boundaries[0] == (7, 2, None, None)
        assert rest[-1].boundaries[0] == (7, 2, None, 417)
        assert rest[-1].processed_samples == 417
        assert service.half_closed
        assert service.configs[0].config.sample_rate_hertz == 16000
        assert service.configs[0].input_buffer_latency_ms == 640
        assert service.audios == [bytes(634), bytes(200)]


@pytest.mark.asyncio
async def test_empty_audio_does_not_open_rpc():
    async def audio():
        if False:
            yield b""

    service = Service()
    async with running(service) as client:
        assert [value async for value in client.stream_diarize(audio(), 16000, input_buffer_latency_ms=320)] == []
    assert service.configs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rate, latency", [(8000, 320), (16000, 321)])
async def test_invalid_configuration(rate, latency):
    async def audio():
        yield bytes(320)

    async with running(Service()) as client:
        with pytest.raises(ValueError):
            await anext(client.stream_diarize(audio(), rate, input_buffer_latency_ms=latency))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["abort", "malformed", "input"])
async def test_duplex_propagates_server_protocol_and_input_errors(failure):
    async def audio():
        yield bytes(320)
        if failure == "input":
            raise ValueError("source failed")

    service = Service(abort=failure == "abort", malformed=failure == "malformed")
    async with running(service) as client:
        with pytest.raises(EngineUnavailableError if failure == "abort" else ValueError):
            async for _ in client.stream_diarize(audio(), 16000, input_buffer_latency_ms=320):
                pass
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "diarization-duplex-send"]


@pytest.mark.asyncio
async def test_consumer_close_cancels_rpc_and_sender():
    async def audio():
        yield bytes(320)
        await asyncio.Event().wait()

    service = Service()
    async with running(service) as client:
        async with aclosing(client.stream_diarize(audio(), 16000, input_buffer_latency_ms=320)) as stream:
            await asyncio.wait_for(anext(stream), 2)
        await asyncio.wait_for(service.finished.wait(), 2)
    assert service.audios == [bytes(320)]
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "diarization-duplex-send"]
