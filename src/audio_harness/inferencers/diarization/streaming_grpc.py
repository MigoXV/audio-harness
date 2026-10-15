from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import grpc

from audio_harness.configs.engine import EngineEndpoint
from audio_harness.inferencers.diarization.streaming_base import StreamingDiarizationInferencer
from audio_harness.inferencers.transports.grpc import GrpcTransport, raise_grpc_error
from audio_harness.protos.diarization import ux_speaker_diarization_pb2 as pb
from audio_harness.protos.diarization import ux_speaker_diarization_pb2_grpc as rpc


def _samples(value, *, boundary: bool = False) -> int | None:
    if boundary and value.seconds == -1 and value.nanos == 0:
        return None
    if value.seconds < 0 or not 0 <= value.nanos < 1_000_000_000:
        raise ValueError("流式分离返回非法 Duration")
    return value.seconds * 16000 + round(value.nanos * 16000 / 1_000_000_000)


class UxStreamingDiarizationInferencer(StreamingDiarizationInferencer):
    def __init__(self, endpoint: EngineEndpoint, *, max_message_bytes: int) -> None:
        self._transport = GrpcTransport(endpoint, max_message_bytes=max_message_bytes)
        self._stub = rpc.UxSpeakerDiarizationStub(self._transport.channel)

    async def ready(self) -> None:
        await self._transport.ready()

    async def probe(self) -> None:
        await self._transport.probe()

    async def close(self) -> None:
        await self._transport.close()

    async def stream_diarize(
        self, audio: AsyncIterator[bytes], sample_rate: int, *, input_buffer_latency_ms: int,
    ) -> AsyncIterator[StreamingDiarizationInferencer.Update]:
        if sample_rate != 16000:
            raise ValueError("流式说话人分离仅支持 16 kHz 单声道 PCM S16LE")
        if input_buffer_latency_ms not in (320, 640, 1040):
            raise ValueError("input_buffer_latency_ms 必须为 320、640 或 1040")
        first = await anext(audio, None)
        if first is None:
            return
        call = self._stub.DuplexStreamingDetect(
            timeout=self._transport.endpoint.request_timeout_seconds,
        )
        send_error: Exception | None = None
        input_closed = False

        async def send() -> None:
            nonlocal send_error, input_closed
            try:
                await call.write(pb.DuplexStreamingDetectRequest(config=pb.DuplexStreamingConfig(
                    config=pb.Config(encoding=pb.Config.LINEAR16, sample_rate_hertz=sample_rate),
                    input_buffer_latency_ms=input_buffer_latency_ms,
                )))
                chunk = first
                while chunk is not None:
                    if not chunk or len(chunk) % 2:
                        raise ValueError("流式分离输入必须是非空 PCM S16LE")
                    await call.write(pb.DuplexStreamingDetectRequest(audio_content=chunk))
                    chunk = await anext(audio, None)
                input_closed = True
                await call.done_writing()
            except Exception as exc:
                send_error = exc
                call.cancel()

        sender = asyncio.create_task(send(), name="diarization-duplex-send")
        try:
            async for response in call:
                processed = _samples(response.process_time)
                assert processed is not None
                yield self.Update(tuple(
                    self.Boundary(
                        value.segment_id, value.speaker,
                        _samples(value.start_time, boundary=True),
                        _samples(value.end_time, boundary=True),
                    ) for value in response.results
                ), processed)
            if send_error is not None:
                raise send_error
            if not input_closed:
                raise ValueError("流式分离在输入结束前关闭了响应流")
            await sender
        except asyncio.CancelledError:
            if send_error is not None:
                if isinstance(send_error, grpc.aio.AioRpcError):
                    raise_grpc_error(send_error, "Streaming Speaker Diarization")
                raise send_error
            raise
        except grpc.aio.AioRpcError as exc:
            raise_grpc_error(exc, "Streaming Speaker Diarization")
        finally:
            call.cancel()
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
