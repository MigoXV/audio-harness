from __future__ import annotations

from collections.abc import AsyncIterator

import grpc

from audio_harness.configs import EngineEndpoint
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.transports.grpc import (
    GrpcTransport,
    raise_grpc_error,
)
from audio_harness.protos.asr import ux_speech_pb2 as speech_pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as speech_pb2_grpc


class UxSpeechAsrInferencer(AsrInferencer):
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
        self.stub = speech_pb2_grpc.UxSpeechStub(self._transport.channel)

    async def ready(self) -> None:
        await self._transport.ready()

    async def probe(self) -> None:
        await self._transport.probe()

    async def close(self) -> None:
        await self._transport.close()

    async def transcribe(
        self,
        audio: bytes,
        *,
        start_sample: int,
        end_sample: int,
        turn_completed: bool,
        options: AsrInferencer.Options,
    ) -> AsyncIterator[AsrInferencer.Result]:
        async def requests() -> AsyncIterator[
            speech_pb2.StreamingRecognizeRequest
        ]:
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
            if audio:
                yield speech_pb2.StreamingRecognizeRequest(
                    audio_content=audio
                )

        call = self.stub.StreamingRecognize(
            requests(),
            timeout=self._transport.endpoint.request_timeout_seconds,
        )
        try:
            async for response in call:
                for result in response.results:
                    alternative = result.alternative
                    yield AsrInferencer.Result(
                        transcript=alternative.transcript,
                        is_final=result.is_final,
                        start_sample=start_sample,
                        end_sample=end_sample,
                        sample_rate=options.sample_rate,
                        turn_completed=turn_completed,
                        language_code=options.language_code,
                        metadata=dict(alternative.metadata),
                    )
        except grpc.aio.AioRpcError as exc:
            raise_grpc_error(exc, "ASR")
