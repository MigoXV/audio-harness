from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from audio_harness.capacity import ResizableConcurrencyLimiter, ResizableSessionLimiter
from audio_harness.domain import PcmChunk, RecognitionOptions
from audio_harness.engines.pool import InferencerPool
from audio_harness.workflows.runtime import (
    AudioInferenceRequest,
    AudioRuntimeConfig,
    AudioWorkflowInferencer,
    AudioWorkflowOutput,
)
from audio_harness.workflows.service import ActiveAudioWorkflow, AudioWorkflowService
from audio_harness.workflow_settings import WorkflowSettingsStore

_AUDIO_END = object()
_OUTPUT_END = object()


@dataclass(frozen=True)
class _Failure:
    error: BaseException


class AudioWorkflowSession:
    """协议无关的单条流式语音工作流会话。"""

    def __init__(
        self,
        *,
        inferencer_pool: InferencerPool,
        workflow: ActiveAudioWorkflow,
        runtime_config: AudioRuntimeConfig,
        audio_queue_capacity: int,
        asr_limiter: ResizableConcurrencyLimiter,
        options: RecognitionOptions,
        include_vad_events: bool,
        include_vad_progress: bool,
        include_interim_transcripts: bool,
    ) -> None:
        self._inferencer_pool = inferencer_pool
        self._workflow = workflow
        self._runtime_config = runtime_config
        self._asr_limiter = asr_limiter
        self._request = AudioInferenceRequest(
            recognition=options,
            include_vad_events=include_vad_events,
            include_vad_progress=include_vad_progress,
            include_interim_transcripts=include_interim_transcripts,
        )
        self._audio: asyncio.Queue[bytes | object] = asyncio.Queue(
            maxsize=audio_queue_capacity
        )
        self._outputs: asyncio.Queue[AudioWorkflowOutput | _Failure | object] = (
            asyncio.Queue(maxsize=runtime_config.result_queue_capacity)
        )
        self._finished = False
        self._closed = False
        self._task = asyncio.create_task(
            self._run(),
            name="audio-workflow-session",
        )

    @property
    def workflow(self) -> ActiveAudioWorkflow:
        return self._workflow

    async def append(self, audio: bytes) -> None:
        if self._finished or self._closed:
            raise RuntimeError("语音工作流会话已经结束")
        if len(audio) % 2:
            raise ValueError("PCM S16LE 音频字节数必须为偶数")
        if audio:
            await self._audio.put(audio)

    async def finish(self) -> None:
        if self._finished or self._closed:
            return
        self._finished = True
        await self._audio.put(_AUDIO_END)

    async def outputs(self) -> AsyncIterator[AudioWorkflowOutput]:
        while True:
            item = await self._outputs.get()
            if item is _OUTPUT_END:
                return
            if isinstance(item, _Failure):
                raise item.error
            assert isinstance(item, AudioWorkflowOutput)
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._task.done():
            self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _audio_chunks(self) -> AsyncIterator[PcmChunk]:
        start_sample = 0
        while True:
            item = await self._audio.get()
            if item is _AUDIO_END:
                return
            assert isinstance(item, bytes)
            chunk = PcmChunk(
                pcm_s16le=item,
                sample_rate=self._request.recognition.sample_rate,
                start_sample=start_sample,
            )
            start_sample = chunk.end_sample
            yield chunk

    async def _run(self) -> None:
        failure: BaseException | None = None
        cancelled = False
        try:
            plan = self._workflow.plan
            async with self._inferencer_pool.lease(plan.engine_snapshots) as leased:
                engines = {
                    node_id: leased[
                        (node.engine_snapshot.id, node.engine_snapshot.revision)
                    ]
                    for node_id, node in plan.nodes.items()
                }
                inferencer = AudioWorkflowInferencer(
                    plan=plan,
                    engines=engines,
                    runtime_config=self._runtime_config,
                    asr_semaphore=self._asr_limiter,
                )
                async for output in inferencer.stream(
                    self._audio_chunks(),
                    self._request,
                ):
                    await self._outputs.put(output)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except BaseException as exc:
            failure = exc
        finally:
            if not cancelled:
                if failure is not None:
                    await self._outputs.put(_Failure(failure))
                await self._outputs.put(_OUTPUT_END)


class AudioWorkflowSessionService:
    def __init__(
        self,
        *,
        inferencer_pool: InferencerPool,
        workflows: AudioWorkflowService,
        workflow_settings: WorkflowSettingsStore,
        session_limiter: ResizableSessionLimiter,
        asr_limiter: ResizableConcurrencyLimiter,
    ) -> None:
        self.inferencer_pool = inferencer_pool
        self.workflows = workflows
        self.workflow_settings = workflow_settings
        self.session_limiter = session_limiter
        self.asr_limiter = asr_limiter

    def default_workflow(self) -> ActiveAudioWorkflow:
        workflow_id = self.workflow_settings.current().grpc_audio_workflow_id
        if workflow_id is None:
            raise RuntimeError("尚未配置 gRPC 默认语音工作流")
        return self.workflows.current(workflow_id)

    @asynccontextmanager
    async def reserve(self):
        async with self.session_limiter.slot():
            reservation = AudioWorkflowSessionReservation(self)
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
        include_interim_transcripts: bool = False,
        workflow: ActiveAudioWorkflow | None = None,
    ):
        async with self.reserve() as reservation:
            session = reservation.create(
                options=options,
                include_vad_events=include_vad_events,
                include_vad_progress=include_vad_progress,
                include_interim_transcripts=include_interim_transcripts,
                workflow=workflow,
            )
            try:
                yield session
            finally:
                await reservation.close_session()


class AudioWorkflowSessionReservation:
    """持有协议会话容量，并允许按需轮转底层语音工作流会话。"""

    def __init__(self, service: AudioWorkflowSessionService) -> None:
        self._service = service
        self._session: AudioWorkflowSession | None = None
        self._closed = False

    def create(
        self,
        *,
        options: RecognitionOptions,
        include_vad_events: bool = False,
        include_vad_progress: bool = False,
        include_interim_transcripts: bool = False,
        workflow: ActiveAudioWorkflow | None = None,
    ) -> AudioWorkflowSession:
        if self._closed:
            raise RuntimeError("会话容量预留已经关闭")
        if self._session is not None:
            raise RuntimeError("当前预留中已经存在语音工作流会话")
        settings = self._service.workflow_settings.current().config
        self._session = AudioWorkflowSession(
            inferencer_pool=self._service.inferencer_pool,
            workflow=workflow or self._service.default_workflow(),
            runtime_config=AudioRuntimeConfig(
                segment_queue_capacity=settings.segment_queue_capacity,
                result_queue_capacity=settings.result_queue_capacity,
            ),
            audio_queue_capacity=settings.audio_queue_capacity,
            asr_limiter=self._service.asr_limiter,
            options=options,
            include_vad_events=include_vad_events,
            include_vad_progress=include_vad_progress,
            include_interim_transcripts=include_interim_transcripts,
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
