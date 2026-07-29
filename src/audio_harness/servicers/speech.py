from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import grpc
from google.protobuf import duration_pb2

from audio_harness.config import AppConfig
from audio_harness.domain import (
    AsyncAsrEngine,
    AsyncVadEngine,
    CapacityExceededError,
    EngineDeadlineError,
    EngineUnavailableError,
    InvalidAudioError,
    RecognitionOptions,
    TranscriptionResult,
)
from audio_harness.pipelines.asr_vad import AsrVadWorkflow
from audio_harness.protos.asr import ux_speech_pb2 as pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as pb2_grpc

logger = logging.getLogger(__name__)

_AUDIO_END = object()


@dataclass(frozen=True)
class _RecognitionSettings:
    options: RecognitionOptions


class _SessionLimiter:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.active = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self):
        async with self._lock:
            if self.active >= self.capacity:
                raise CapacityExceededError(
                    f"活动会话已达到上限: {self.capacity}"
                )
            self.active += 1
        try:
            yield
        finally:
            async with self._lock:
                self.active -= 1


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
        asr: AsyncAsrEngine,
        vad: AsyncVadEngine,
        semantic_vad: AsyncVadEngine | None,
    ) -> None:
        self.config = config
        self.asr = asr
        self.vad = vad
        self.semantic_vad = semantic_vad
        self.session_limiter = _SessionLimiter(config.workflow.max_sessions)
        self.asr_semaphore = asyncio.Semaphore(
            config.workflow.max_asr_concurrency
        )

    async def StreamingRecognize(
        self,
        request_iterator: AsyncIterator[pb2.StreamingRecognizeRequest],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[pb2.StreamingRecognizeResponse]:
        reader: asyncio.Task[None] | None = None
        try:
            async with self.session_limiter.slot():
                try:
                    first_request = await anext(request_iterator)
                except StopAsyncIteration as exc:
                    raise InvalidAudioError("请求流不能为空") from exc
                settings = self._parse_first_request(first_request)
                audio_queue: asyncio.Queue[object] = asyncio.Queue(
                    maxsize=self.config.workflow.audio_queue_capacity
                )
                reader = asyncio.create_task(
                    self._read_audio(request_iterator, audio_queue),
                    name="audio-harness-grpc-reader",
                )
                workflow = AsrVadWorkflow(
                    asr=self.asr,
                    vad=self.vad,
                    semantic_vad=self.semantic_vad,
                    config=self.config.workflow,
                    asr_semaphore=self.asr_semaphore,
                )

                async for result in workflow.run(
                    self._audio_queue_iterator(audio_queue),
                    settings.options,
                ):
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
            language = self.config.workflow.default_language_code
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
        audio_queue: asyncio.Queue[object],
    ) -> None:
        buffer = bytearray()
        chunk_size = self.config.workflow.min_chunk_bytes
        try:
            async for request in request_iterator:
                request_type = request.WhichOneof("streaming_request")
                if request_type != "audio_content":
                    raise InvalidAudioError(
                        "首包之后只能发送 audio_content"
                    )
                chunk = request.audio_content
                if len(chunk) > self.config.serving.max_message_bytes:
                    raise InvalidAudioError("单个音频消息超过 max_message_bytes")
                buffer.extend(chunk)
                while len(buffer) >= chunk_size:
                    await audio_queue.put(bytes(buffer[:chunk_size]))
                    del buffer[:chunk_size]
            if len(buffer) % 2:
                raise InvalidAudioError("PCM S16LE 总字节数必须为偶数")
            if buffer:
                await audio_queue.put(bytes(buffer))
        finally:
            await audio_queue.put(_AUDIO_END)

    async def _audio_queue_iterator(
        self,
        audio_queue: asyncio.Queue[object],
    ) -> AsyncIterator[bytes]:
        while True:
            item = await audio_queue.get()
            if item is _AUDIO_END:
                return
            assert isinstance(item, bytes)
            yield item
