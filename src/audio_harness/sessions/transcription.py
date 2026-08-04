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
)
from audio_harness.engines.router import EngineRouter
from audio_harness.pipelines.graph_runtime import (
    GraphRuntimeConfig,
    GraphSessionRunner,
)
from audio_harness.pipelines.dag import CompiledPipelinePlan
from audio_harness.pipelines.service import ActivePipelineSnapshot, PipelineStore
from audio_harness.workflow_settings import WorkflowSettingsStore

_AUDIO_END = object()
_EVENT_END = object()

SessionEvent = (
    TranscriptionResult
    | SpeechStarted
    | SpeechStopped
)

_TRANSCRIPTION_NODE_TYPES = {
    "audio_input",
    "physical_vad",
    "semantic_vad",
    "asr",
    "session_output",
}


def transcription_plan(plan: CompiledPipelinePlan) -> CompiledPipelinePlan:
    """从语音 Agent 图中提取 ASR/VAD 前半链。"""
    nodes = {
        node_id: node
        for node_id, node in plan.nodes.items()
        if node.node_type.type in _TRANSCRIPTION_NODE_TYPES
    }
    output_ids = [
        node_id
        for node_id, node in nodes.items()
        if node.node_type.type == "session_output"
    ]
    if len(output_ids) != 1:
        raise RuntimeError("工作流必须包含一个 session_output 节点")
    output_id = output_ids[0]
    edges = [
        edge
        for edge in plan.edges
        if edge.source_node in nodes and edge.target_node in nodes
    ]
    return CompiledPipelinePlan(
        schema_version=plan.schema_version,
        definition=plan.definition,
        nodes=nodes,
        edges=tuple(edges),
        topological_order=tuple(
            node_id
            for node_id in plan.topological_order
            if node_id in nodes
        ),
    )


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
        pipeline: ActivePipelineSnapshot | None = None,
    ) -> None:
        self._engine_router = engine_router
        self._pipeline_store = pipeline_store
        self._config = workflow_settings.current().config
        self._asr_limiter = asr_limiter
        self._options = options
        self._include_vad_events = include_vad_events
        self._include_vad_progress = include_vad_progress
        self._pipeline = pipeline
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
                (
                    TranscriptionResult,
                    SpeechStarted,
                    SpeechStopped,
                ),
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
            pipeline = self._pipeline or self._pipeline_store.current()
            plan = transcription_plan(pipeline.plan)
            async with self._engine_router.lease_snapshots(
                plan.engine_snapshots
            ) as leased:
                engines = {
                    node_id: leased[
                        (
                            node.engine_snapshot.id,
                            node.engine_snapshot.revision,
                        )
                    ]
                    for node_id, node in plan.nodes.items()
                    if node.engine_snapshot is not None
                }
                runner = GraphSessionRunner(
                    plan=plan,
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
        pipeline: ActivePipelineSnapshot | None = None,
    ):
        async with self.reserve() as reservation:
            session = reservation.create(
                options=options,
                include_vad_events=include_vad_events,
                include_vad_progress=include_vad_progress,
                pipeline=pipeline,
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
        pipeline: ActivePipelineSnapshot | None = None,
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
            pipeline=pipeline,
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
