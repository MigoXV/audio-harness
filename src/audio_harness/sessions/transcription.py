from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from audio_harness.capacity import (
    ResizableConcurrencyLimiter,
    ResizableSessionLimiter,
)
from audio_harness.domain import (
    EngineLeaseProvider,
    RecognitionOptions,
    SpeechStarted,
    SpeechStopped,
    TranscriptionResult,
    VadUpdate,
)
from audio_harness.pipelines.asr_vad import AsrVadWorkflow
from audio_harness.workflow_settings import WorkflowSettingsStore

_AUDIO_END = object()
_EVENT_END = object()

SessionEvent = TranscriptionResult | SpeechStarted | SpeechStopped | VadUpdate


@dataclass(frozen=True)
class _Failure:
    error: BaseException


class TranscriptionSession:
    """协议无关的单条流式转写会话。"""

    def __init__(
        self,
        *,
        engine_provider: EngineLeaseProvider,
        workflow_settings: WorkflowSettingsStore,
        asr_limiter: ResizableConcurrencyLimiter,
        options: RecognitionOptions,
        include_vad_events: bool,
        include_vad_progress: bool,
        use_semantic_vad: bool,
    ) -> None:
        self._engine_provider = engine_provider
        self._config = workflow_settings.current().config
        self._asr_limiter = asr_limiter
        self._options = options
        self._include_vad_events = include_vad_events
        self._include_vad_progress = include_vad_progress
        self._use_semantic_vad = use_semantic_vad
        self._audio: asyncio.Queue[bytes | object] = asyncio.Queue(
            maxsize=self._config.audio_queue_capacity
        )
        self._events: asyncio.Queue[SessionEvent | object] = asyncio.Queue(
            maxsize=self._config.result_queue_capacity
        )
        self._finished = False
        self._closed = False
        self._task = asyncio.create_task(
            self._run(),
            name="audio-harness-transcription-session",
        )

    async def append(self, audio: bytes) -> None:
        if self._finished or self._closed:
            raise RuntimeError("转写会话已经结束")
        if len(audio) % 2:
            raise ValueError("PCM S16LE 音频字节数必须为偶数")
        if audio:
            await self._audio.put(audio)

    async def finish(self) -> None:
        if self._finished or self._closed:
            return
        self._finished = True
        await self._audio.put(_AUDIO_END)

    async def events(self) -> AsyncIterator[SessionEvent]:
        while True:
            item = await self._events.get()
            if item is _EVENT_END:
                return
            if isinstance(item, _Failure):
                raise item.error
            assert isinstance(
                item,
                (TranscriptionResult, SpeechStarted, SpeechStopped, VadUpdate),
            )
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._task.done():
            self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _audio_chunks(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._audio.get()
            if item is _AUDIO_END:
                return
            assert isinstance(item, bytes)
            yield item

    async def _run(self) -> None:
        failure: BaseException | None = None
        cancelled = False
        try:
            async with self._engine_provider.lease() as engines:
                workflow = AsrVadWorkflow(
                    asr=engines.asr,
                    vad=engines.vad,
                    semantic_vad=(
                        engines.semantic_vad
                        if self._use_semantic_vad
                        else None
                    ),
                    config=self._config,
                    asr_semaphore=self._asr_limiter,
                )
                async for event in workflow.run(
                    self._audio_chunks(),
                    self._options,
                    include_vad_events=self._include_vad_events,
                    include_vad_progress=self._include_vad_progress,
                ):
                    await self._events.put(event)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except BaseException as exc:
            failure = exc
        finally:
            if failure is not None:
                await self._events.put(_Failure(failure))
            if not cancelled:
                await self._events.put(_EVENT_END)


class TranscriptionSessionService:
    def __init__(
        self,
        *,
        engine_provider: EngineLeaseProvider,
        workflow_settings: WorkflowSettingsStore,
        session_limiter: ResizableSessionLimiter,
        asr_limiter: ResizableConcurrencyLimiter,
    ) -> None:
        self._engine_provider = engine_provider
        self._workflow_settings = workflow_settings
        self._session_limiter = session_limiter
        self._asr_limiter = asr_limiter

    @asynccontextmanager
    async def reserve(self):
        async with self._session_limiter.slot():
            reservation = TranscriptionSessionReservation(self)
            try:
                yield reservation
            finally:
                await reservation.close()

    @asynccontextmanager
    async def open(
        self,
        options: RecognitionOptions,
        *,
        include_vad_events: bool = False,
        include_vad_progress: bool = False,
        use_semantic_vad: bool = True,
    ):
        async with self.reserve() as reservation:
            session = reservation.create(
                options=options,
                include_vad_events=include_vad_events,
                include_vad_progress=include_vad_progress,
                use_semantic_vad=use_semantic_vad,
            )
            try:
                yield session
            finally:
                await reservation.close_session()


class TranscriptionSessionReservation:
    """持有协议会话容量，并允许按需轮转底层转写会话。"""

    def __init__(self, service: TranscriptionSessionService) -> None:
        self._service = service
        self._session: TranscriptionSession | None = None
        self._closed = False

    def create(
        self,
        *,
        options: RecognitionOptions,
        include_vad_events: bool = False,
        include_vad_progress: bool = False,
        use_semantic_vad: bool = True,
    ) -> TranscriptionSession:
        if self._closed:
            raise RuntimeError("会话容量预留已经关闭")
        if self._session is not None:
            raise RuntimeError("当前预留中已经存在转写会话")
        self._session = TranscriptionSession(
            engine_provider=self._service._engine_provider,
            workflow_settings=self._service._workflow_settings,
            asr_limiter=self._service._asr_limiter,
            options=options,
            include_vad_events=include_vad_events,
            include_vad_progress=include_vad_progress,
            use_semantic_vad=use_semantic_vad,
        )
        return self._session

    async def close_session(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            await session.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.close_session()
