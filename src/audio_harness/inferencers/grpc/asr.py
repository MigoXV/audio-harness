from __future__ import annotations

from collections.abc import AsyncIterator

import grpc

from audio_harness.config import EngineEndpoint
from audio_harness.domain import (
    AudioSegment,
    RecognitionOptions,
    TranscriptionResult,
)
from audio_harness.inferencers.grpc.base import (
    GrpcInferencerBase,
    raise_grpc_error,
)
from audio_harness.protos.asr import ux_speech_pb2 as speech_pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as speech_pb2_grpc


class GrpcAsrInferencer(GrpcInferencerBase):
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
            if segment.audio:
                yield speech_pb2.StreamingRecognizeRequest(
                    audio_content=segment.audio
                )

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
            raise_grpc_error(exc, "ASR")
