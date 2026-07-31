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
    RecognitionOptions,
    SpeechStarted,
    SpeechStopped,
    TranscriptionResult,
    VadUpdate,
)
from audio_harness.engines.router import EngineRouter
from audio_harness.pipelines.graph_runtime import (
    GraphRuntimeConfig,
    GraphSessionRunner,
)
from audio_harness.pipelines.service import PipelineStore
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
        engine_router: EngineRouter,
        pipeline_store: PipelineStore,
        workflow_settings: WorkflowSettingsStore,
        asr_limiter: ResizableConcurrencyLimiter,
        options: RecognitionOptions,
        include_vad_events: bool,
        include_vad_progress: bool,
    ) -> None:
        self._engine_router = engine_router
        self._pipeline_store = pipeline_store
        self._config = workflow_settings.current().config
        self._asr_limiter = asr_limiter
        self._options = options
        self._include_vad_events = include_vad_events
        self._include_vad_progress = include_vad_progress
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
            pipeline = self._pipeline_store.current()
            async with self._engine_router.lease_snapshots(
                pipeline.plan.engine_snapshots
            ) as leased:
                engines = {
                    node_id: leased[
                        (
                            node.engine_snapshot.id,
                            node.engine_snapshot.revision,
                        )
                    ]
                    for node_id, node in pipeline.plan.nodes.items()
                    if node.engine_snapshot is not None
                }
                runner = GraphSessionRunner(
                    plan=pipeline.plan,
                    engines=engines,
                    options=self._options,
                    runtime_config=GraphRuntimeConfig(
                        segment_queue_capacity=(
                            self._config.segment_queue_capacity
                        ),
                        result_queue_capacity=(
                            self._config.result_queue_capacity
                        ),
                    ),
                    asr_semaphore=self._asr_limiter,
                    include_vad_events=self._include_vad_events,
                    include_vad_progress=self._include_vad_progress,
                )
                async for event in runner.run(
                    self._audio_chunks(),
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
        engine_router: EngineRouter,
        pipeline_store: PipelineStore,
        workflow_settings: WorkflowSettingsStore,
        session_limiter: ResizableSessionLimiter,
        asr_limiter: ResizableConcurrencyLimiter,
    ) -> None:
        self._engine_router = engine_router
        self._pipeline_store = pipeline_store
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
    ):
        async with self.reserve() as reservation:
            session = reservation.create(
                options=options,
                include_vad_events=include_vad_events,
                include_vad_progress=include_vad_progress,
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
    ) -> TranscriptionSession:
        if self._closed:
            raise RuntimeError("会话容量预留已经关闭")
        if self._session is not None:
            raise RuntimeError("当前预留中已经存在转写会话")
        self._session = TranscriptionSession(
            engine_router=self._service._engine_router,
            pipeline_store=self._service._pipeline_store,
            workflow_settings=self._service._workflow_settings,
            asr_limiter=self._service._asr_limiter,
            options=options,
            include_vad_events=include_vad_events,
            include_vad_progress=include_vad_progress,
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
