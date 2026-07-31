from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import grpc
from google.protobuf import duration_pb2

from audio_harness.config import AppConfig
from audio_harness.domain import (
    CapacityExceededError,
    EngineConfigurationInvalidError,
    EngineDeadlineError,
    EngineUnavailableError,
    InvalidAudioError,
    RecognitionOptions,
    TranscriptionResult,
)
from audio_harness.protos.asr import ux_speech_pb2 as pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as pb2_grpc
from audio_harness.pipelines.service import PipelineNotActiveError
from audio_harness.sessions import (
    TranscriptionSession,
    TranscriptionSessionService,
)
from audio_harness.workflow_settings import WorkflowSettingsStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RecognitionSettings:
    options: RecognitionOptions


def _seconds_to_duration(value: float) -> duration_pb2.Duration:
    seconds = int(value)
    nanos = int(round((value - seconds) * 1_000_000_000))
    if nanos == 1_000_000_000:
        seconds += 1
        nanos = 0
    return duration_pb2.Duration(seconds=seconds, nanos=nanos)


def build_response(result: TranscriptionResult) -> pb2.StreamingRecognizeResponse:
    start = result.start_sample / result.sample_rate
    end = result.end_sample / result.sample_rate
    word = pb2.WordInfo(
        word=result.transcript,
        start_time=_seconds_to_duration(start),
        end_time=_seconds_to_duration(end),
    )
    alternative = pb2.SpeechRecognitionAlternative(
        transcript=result.transcript,
        words=[word],
        metadata=result.metadata,
        turn_completed=result.turn_completed,
    )
    if result.language_code:
        alternative.language.CopyFrom(pb2.LanguageInfo(code=result.language_code))
    return pb2.StreamingRecognizeResponse(
        results=[
            pb2.StreamingRecognitionResult(
                alternative=alternative,
                is_final=result.is_final,
            )
        ]
    )


class SpeechServicer(pb2_grpc.UxSpeechServicer):
    def __init__(
        self,
        *,
        config: AppConfig,
        session_service: TranscriptionSessionService,
        workflow_settings: WorkflowSettingsStore | None = None,
    ) -> None:
        self.config = config
        self.session_service = session_service
        self.workflow_settings = workflow_settings

    async def StreamingRecognize(
        self,
        request_iterator: AsyncIterator[pb2.StreamingRecognizeRequest],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[pb2.StreamingRecognizeResponse]:
        reader: asyncio.Task[None] | None = None
        try:
            workflow_config = (
                self.workflow_settings.current().config
                if self.workflow_settings is not None
                else self.config.workflow
            )
            try:
                first_request = await anext(request_iterator)
            except StopAsyncIteration as exc:
                raise InvalidAudioError("请求流不能为空") from exc
            settings = self._parse_first_request(
                first_request,
                default_language_code=workflow_config.default_language_code,
            )
            async with self.session_service.open(
                settings.options
            ) as session:
                reader = asyncio.create_task(
                    self._read_audio(
                        request_iterator,
                        session,
                        min_chunk_bytes=workflow_config.min_chunk_bytes,
                    ),
                    name="audio-harness-grpc-reader",
                )
                async for result in session.events():
                    if not isinstance(result, TranscriptionResult):
                        continue
                    yield build_response(result)
                await reader
        except CapacityExceededError as exc:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(exc))
        except InvalidAudioError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except EngineDeadlineError as exc:
            await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, str(exc))
        except EngineUnavailableError as exc:
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        except (
            EngineConfigurationInvalidError,
            PipelineNotActiveError,
        ) as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except asyncio.CancelledError:
            logger.info("客户端取消 StreamingRecognize")
            raise
        except grpc.aio.AbortError:
            raise
        except Exception as exc:
            logger.exception("StreamingRecognize 处理失败")
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
        finally:
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    def _parse_first_request(
        self,
        request: pb2.StreamingRecognizeRequest,
        *,
        default_language_code: str,
    ) -> _RecognitionSettings:
        if not request.HasField("streaming_config"):
            raise InvalidAudioError("首包必须包含 streaming_config")
        config = request.streaming_config.config
        if config.encoding not in (
            pb2.RecognitionConfig.AUDIO_ENCODING_UNSPECIFIED,
            pb2.RecognitionConfig.LINEAR16,
        ):
            raise InvalidAudioError("第一版只支持 LINEAR16")
        sample_rate = config.sample_rate_hertz or 16000
        if sample_rate <= 0:
            raise InvalidAudioError("sample_rate_hertz 必须大于 0")
        language = config.language_code.strip()
        if not language or language.lower() == "auto":
            language = default_language_code
        return _RecognitionSettings(
            options=RecognitionOptions(
                sample_rate=sample_rate,
                language_code=language,
                enable_automatic_punctuation=config.enable_automatic_punctuation,
                hotwords=tuple(config.hotwords),
                hotword_bias=config.hotword_bias,
            )
        )

    async def _read_audio(
        self,
        request_iterator: AsyncIterator[pb2.StreamingRecognizeRequest],
        session: TranscriptionSession,
        *,
        min_chunk_bytes: int,
    ) -> None:
        buffer = bytearray()
        chunk_size = min_chunk_bytes
        try:
            async for request in request_iterator:
                request_type = request.WhichOneof("streaming_request")
                if request_type != "audio_content":
                    raise InvalidAudioError(
                        "首包之后只能发送 audio_content"
                    )
                chunk = request.audio_content
                if len(chunk) > self.config.limits.max_message_bytes:
                    raise InvalidAudioError("单个音频消息超过 max_message_bytes")
                buffer.extend(chunk)
                while len(buffer) >= chunk_size:
                    await session.append(bytes(buffer[:chunk_size]))
                    del buffer[:chunk_size]
            if len(buffer) % 2:
                raise InvalidAudioError("PCM S16LE 总字节数必须为偶数")
            if buffer:
                await session.append(bytes(buffer))
        finally:
            await session.finish()
