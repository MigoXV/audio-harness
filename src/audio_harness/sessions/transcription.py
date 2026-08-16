from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from audio_harness.engines.pool import InferencerPool
from audio_harness.inferencers import Inferencer
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.limits import ResizableConcurrencyLimiter
from audio_harness.media.pcm import PcmChunk
from audio_harness.sessions.limits import ResizableSessionLimiter
from audio_harness.workflows.runtime import (
    AudioInferenceRequest,
    AudioRuntimeConfig,
    AudioWorkflowRuntime,
    AudioWorkflowOutput,
)
from audio_harness.workflows.service import ActiveAudioWorkflow, AudioWorkflowService
from audio_harness.workflows.settings import WorkflowSettingsStore
from audio_harness.vectors.service import VectorService

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
        inferencers: dict[str, Inferencer],
        workflow: ActiveAudioWorkflow,
        runtime_config: AudioRuntimeConfig,
        audio_queue_capacity: int,
        asr_limiter: ResizableConcurrencyLimiter,
        embedding_limiter: ResizableConcurrencyLimiter | None = None,
        options: AsrInferencer.Options,
        include_vad_events: bool,
        include_vad_progress: bool,
        include_interim_transcripts: bool,
        include_vector_search: bool = False,
        include_diarization: bool = False,
        vectors: VectorService | None = None,
    ) -> None:
        self._inferencers = dict(inferencers)
        self._workflow = workflow
        self._runtime_config = runtime_config
        self._asr_limiter = asr_limiter
        self._embedding_limiter = embedding_limiter
        self._request = AudioInferenceRequest(
            recognition=options,
            include_vad_events=include_vad_events,
            include_vad_progress=include_vad_progress,
            include_interim_transcripts=include_interim_transcripts,
            include_vector_search=include_vector_search,
            include_diarization=include_diarization,
        )
        self._audio: asyncio.Queue[bytes | object] = asyncio.Queue(
            maxsize=audio_queue_capacity
        )
        self._outputs: asyncio.Queue[AudioWorkflowOutput | _Failure | object] = (
            asyncio.Queue(maxsize=runtime_config.result_queue_capacity)
        )
        self._finished = False
        self._closed = False
        self._vectors = vectors
        self._task: asyncio.Task[None] | None = None

    @property
    def workflow(self) -> ActiveAudioWorkflow:
        return self._workflow

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("语音工作流会话已经关闭")
        if self._task is not None:
            raise RuntimeError("语音工作流会话已经启动")
        runtime = AudioWorkflowRuntime(
            plan=self._workflow.plan,
            inferencers=self._inferencers,
            runtime_config=self._runtime_config,
            asr_semaphore=self._asr_limiter,
            embedding_semaphore=self._embedding_limiter,
            vectors=self._vectors,
        )
        self._task = asyncio.create_task(
            self._run(runtime),
            name="audio-workflow-session",
        )

    async def append(self, audio: bytes) -> None:
        if self._task is None:
            raise RuntimeError("语音工作流会话尚未启动")
        if self._finished or self._closed:
            raise RuntimeError("语音工作流会话已经结束")
        if len(audio) % 2:
            raise ValueError("PCM S16LE 音频字节数必须为偶数")
        if audio:
            await self._audio.put(audio)

    async def finish(self) -> None:
        if self._task is None:
            raise RuntimeError("语音工作流会话尚未启动")
        if self._finished or self._closed:
            return
        self._finished = True
        await self._audio.put(_AUDIO_END)

    async def outputs(self) -> AsyncIterator[AudioWorkflowOutput]:
        if self._task is None:
            raise RuntimeError("语音工作流会话尚未启动")
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
        task = self._task
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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

    async def _run(self, runtime: AudioWorkflowRuntime) -> None:
        failure: BaseException | None = None
        cancelled = False
        try:
            async for output in runtime.stream(
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
        embedding_limiter: ResizableConcurrencyLimiter | None = None,
        vectors: VectorService | None = None,
    ) -> None:
        self.inferencer_pool = inferencer_pool
        self.workflows = workflows
        self.workflow_settings = workflow_settings
        self.session_limiter = session_limiter
        self.asr_limiter = asr_limiter
        self.embedding_limiter = embedding_limiter
        self.vectors = vectors

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
        options: AsrInferencer.Options,
        *,
        include_vad_events: bool = False,
        include_vad_progress: bool = False,
        include_interim_transcripts: bool = False,
        include_vector_search: bool = False,
        include_diarization: bool = False,
        workflow: ActiveAudioWorkflow | None = None,
    ):
        async with self.reserve() as reservation:
            session = await reservation.create(
                options=options,
                include_vad_events=include_vad_events,
                include_vad_progress=include_vad_progress,
                include_interim_transcripts=include_interim_transcripts,
                include_vector_search=include_vector_search,
                include_diarization=include_diarization,
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
        self._lease: AbstractAsyncContextManager[
            dict[tuple[str, int], Inferencer]
        ] | None = None
        self._closed = False

    async def create(
        self,
        *,
        options: AsrInferencer.Options,
        include_vad_events: bool = False,
        include_vad_progress: bool = False,
        include_interim_transcripts: bool = False,
        include_vector_search: bool = False,
        include_diarization: bool = False,
        workflow: ActiveAudioWorkflow | None = None,
    ) -> AudioWorkflowSession:
        if self._closed:
            raise RuntimeError("会话容量预留已经关闭")
        if self._session is not None:
            raise RuntimeError("当前预留中已经存在语音工作流会话")
        settings = self._service.workflow_settings.current().config
        active_workflow = workflow or self._service.default_workflow()
        plan = active_workflow.plan
        lease = self._service.inferencer_pool.lease(plan.engine_snapshots)
        leased = await lease.__aenter__()
        session: AudioWorkflowSession | None = None
        try:
            inferencers = {
                node_id: leased[
                    (node.engine_snapshot.id, node.engine_snapshot.revision)
                ]
                for node_id, node in plan.nodes.items()
                if node.engine_snapshot is not None
            }
            session = AudioWorkflowSession(
                inferencers=inferencers,
                workflow=active_workflow,
                runtime_config=AudioRuntimeConfig(
                    segment_queue_capacity=settings.segment_queue_capacity,
                    result_queue_capacity=settings.result_queue_capacity,
                ),
                audio_queue_capacity=settings.audio_queue_capacity,
                asr_limiter=self._service.asr_limiter,
                embedding_limiter=self._service.embedding_limiter,
                options=options,
                include_vad_events=include_vad_events,
                include_vad_progress=include_vad_progress,
                include_interim_transcripts=include_interim_transcripts,
                include_vector_search=include_vector_search,
                include_diarization=include_diarization,
                vectors=self._service.vectors,
            )
            await session.start()
        except BaseException:
            if session is not None:
                await session.close()
            await lease.__aexit__(None, None, None)
            raise
        self._lease = lease
        self._session = session
        return session

    async def close_session(self) -> None:
        session = self._session
        lease = self._lease
        self._session = None
        self._lease = None
        try:
            if session is not None:
                await session.close()
        finally:
            if lease is not None:
                await lease.__aexit__(None, None, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.close_session()
