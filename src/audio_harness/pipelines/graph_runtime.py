from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import cast

from audio_harness.domain import (
    AsyncAsrEngine,
    AsyncVadEngine,
    AudioSegment,
    RecognitionOptions,
    SpeechStarted,
    SpeechStopped,
    TranscriptionResult,
    VadUpdate,
)
from audio_harness.inferencers.contracts import ManagedInferencer
from audio_harness.pipelines.asr_vad import _RollingPcmBuffer
from audio_harness.pipelines.dag import (
    CompiledNode,
    CompiledPipelinePlan,
    FailurePolicy,
    PhysicalVadNodeConfig,
    SemanticVadNodeConfig,
)

logger = logging.getLogger(__name__)

_EDGE_END = object()
_OUTPUT_END = object()

SessionEvent = (
    TranscriptionResult
    | SpeechStarted
    | SpeechStopped
)


@dataclass(frozen=True)
class GraphEnvelope:
    sequence: int
    source_node: str
    source_port: str
    payload: object


@dataclass(frozen=True)
class GraphAudioSegment:
    segment: AudioSegment
    evaluate_turn: bool = True


@dataclass(frozen=True)
class GraphRuntimeConfig:
    segment_queue_capacity: int
    result_queue_capacity: int


class GraphExecutionError(RuntimeError):
    def __init__(self, node_id: str, cause: BaseException) -> None:
        self.node_id = node_id
        self.cause = cause
        super().__init__(f"节点 {node_id} 执行失败: {cause}")


class _Sequence:
    def __init__(self) -> None:
        self._value = 0

    def next(self) -> int:
        value = self._value
        self._value += 1
        return value


class _EdgeRouter:
    def __init__(
        self,
        plan: CompiledPipelinePlan,
        *,
        config: GraphRuntimeConfig,
    ) -> None:
        self._sequence = _Sequence()
        self._outgoing: dict[
            tuple[str, str],
            list[asyncio.Queue[GraphEnvelope | object]],
        ] = {}
        self._incoming: dict[
            tuple[str, str],
            list[asyncio.Queue[GraphEnvelope | object]],
        ] = {}
        for edge in plan.edges:
            source_type = plan.nodes[edge.source_node].node_type
            port_type = source_type.output_ports[edge.source_port].payload_type
            capacity = (
                config.segment_queue_capacity
                if port_type in {"audio_chunk", "audio_segment"}
                else config.result_queue_capacity
            )
            queue: asyncio.Queue[GraphEnvelope | object] = asyncio.Queue(
                maxsize=capacity
            )
            self._outgoing.setdefault(
                (edge.source_node, edge.source_port),
                [],
            ).append(queue)
            self._incoming.setdefault(
                (edge.target_node, edge.target_port),
                [],
            ).append(queue)

    async def emit(
        self,
        node_id: str,
        port: str,
        payload: object,
    ) -> None:
        envelope = GraphEnvelope(
            sequence=self._sequence.next(),
            source_node=node_id,
            source_port=port,
            payload=payload,
        )
        for queue in self._outgoing.get((node_id, port), ()):
            await queue.put(envelope)

    async def close_node(
        self,
        node_id: str,
        *,
        discard: bool = False,
    ) -> None:
        queues: set[asyncio.Queue[GraphEnvelope | object]] = set()
        for (source_node, _port), values in self._outgoing.items():
            if source_node == node_id:
                queues.update(values)
        for queue in queues:
            if discard:
                while not queue.empty():
                    queue.get_nowait()
            await queue.put(_EDGE_END)

    def input_queues(
        self,
        node_id: str,
        port: str,
    ) -> tuple[asyncio.Queue[GraphEnvelope | object], ...]:
        return tuple(self._incoming.get((node_id, port), ()))


async def _single_input(
    queue: asyncio.Queue[GraphEnvelope | object],
) -> AsyncIterator[GraphEnvelope]:
    while True:
        item = await queue.get()
        if item is _EDGE_END:
            return
        assert isinstance(item, GraphEnvelope)
        yield item


async def _merged_input(
    queues: Iterable[asyncio.Queue[GraphEnvelope | object]],
) -> AsyncIterator[GraphEnvelope]:
    values = tuple(queues)
    if not values:
        return
    merged: asyncio.Queue[tuple[int, GraphEnvelope | object]] = asyncio.Queue()

    async def forward(
        index: int,
        source: asyncio.Queue[GraphEnvelope | object],
    ) -> None:
        while True:
            item = await source.get()
            await merged.put((index, item))
            if item is _EDGE_END:
                return

    tasks = [
        asyncio.create_task(
            forward(index, queue),
            name=f"graph-merge-reader-{index}",
        )
        for index, queue in enumerate(values)
    ]
    ended: set[int] = set()
    try:
        while len(ended) < len(values):
            index, item = await merged.get()
            if item is _EDGE_END:
                ended.add(index)
                continue
            assert isinstance(item, GraphEnvelope)
            yield item
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class GraphSessionRunner:
    def __init__(
        self,
        *,
        plan: CompiledPipelinePlan,
        engines: dict[str, ManagedInferencer],
        options: RecognitionOptions,
        runtime_config: GraphRuntimeConfig,
        asr_semaphore: asyncio.Semaphore,
        include_vad_events: bool,
        include_vad_progress: bool,
    ) -> None:
        self._plan = plan
        self._engines = engines
        self._options = options
        self._runtime_config = runtime_config
        self._asr_semaphore = asr_semaphore
        self._include_vad_events = include_vad_events
        self._include_vad_progress = include_vad_progress
        self._output: asyncio.Queue[SessionEvent | BaseException | object] = (
            asyncio.Queue(maxsize=runtime_config.result_queue_capacity)
        )

    async def run(
        self,
        audio_chunks: AsyncIterator[bytes],
    ) -> AsyncIterator[SessionEvent]:
        router = _EdgeRouter(self._plan, config=self._runtime_config)
        tasks = [
            asyncio.create_task(
                self._run_node(
                    node_id,
                    self._plan.nodes[node_id],
                    router,
                    audio_chunks=audio_chunks,
                ),
                name=f"graph-node-{node_id}",
            )
            for node_id in self._plan.topological_order
        ]
        supervisor = asyncio.create_task(
            self._supervise(tasks),
            name="graph-session-supervisor",
        )
        try:
            while True:
                item = await self._output.get()
                if item is _OUTPUT_END:
                    return
                if isinstance(item, BaseException):
                    raise item
                assert isinstance(
                    item,
                    (
                        TranscriptionResult,
                        SpeechStarted,
                        SpeechStopped,
                    ),
                )
                yield item
        finally:
            if not supervisor.done():
                supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)

    async def _supervise(self, tasks: list[asyncio.Task[None]]) -> None:
        failure: BaseException | None = None
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    failure = task.exception()
                    break
            if failure is not None:
                for task in pending:
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            if failure is not None:
                await self._output.put(failure)
            await self._output.put(_OUTPUT_END)

    async def _run_node(
        self,
        node_id: str,
        node: CompiledNode,
        router: _EdgeRouter,
        *,
        audio_chunks: AsyncIterator[bytes],
    ) -> None:
        failed = False
        try:
            if node.node_type.type == "audio_input":
                await self._audio_input(node_id, router, audio_chunks)
            elif node.node_type.type == "physical_vad":
                await self._physical_vad(node_id, node, router)
            elif node.node_type.type == "semantic_vad":
                await self._semantic_vad(node_id, node, router)
            elif node.node_type.type == "asr":
                await self._asr(node_id, router)
            elif node.node_type.type == "session_output":
                await self._session_output(node_id, router)
            else:
                raise RuntimeError(f"没有节点执行器: {node.node_type.type}")
        except asyncio.CancelledError:
            failed = True
            raise
        except BaseException as exc:
            failed = True
            raise GraphExecutionError(node_id, exc) from exc
        finally:
            await router.close_node(node_id, discard=failed)

    async def _audio_input(
        self,
        node_id: str,
        router: _EdgeRouter,
        audio_chunks: AsyncIterator[bytes],
    ) -> None:
        async for chunk in audio_chunks:
            if len(chunk) % 2:
                raise ValueError("PCM S16LE 音频字节数必须为偶数")
            if chunk:
                await router.emit(node_id, "audio", chunk)

    async def _physical_vad(
        self,
        node_id: str,
        node: CompiledNode,
        router: _EdgeRouter,
    ) -> None:
        input_queues = router.input_queues(node_id, "audio")
        if len(input_queues) != 1:
            raise RuntimeError("物理 VAD 必须有一个音频输入")
        engine = cast(AsyncVadEngine, self._engines[node_id])
        config = cast(PhysicalVadNodeConfig, node.config)
        sample_rate = self._options.sample_rate
        min_samples = int(round(config.min_segment_seconds * sample_rate))
        max_samples = max(
            1,
            int(round(config.max_segment_seconds * sample_rate)),
        )
        pre_roll_samples = int(
            round(config.speech_pre_roll_seconds * sample_rate)
        )
        buffer_samples = max(
            1,
            int(round(config.audio_buffer_seconds * sample_rate)),
        )
        audio = _RollingPcmBuffer(buffer_samples)
        active_start: int | None = None
        last_processed = 0
        has_semantic_vad = any(
            edge.source_node == node_id
            and edge.source_port == "segments"
            and self._plan.nodes[edge.target_node].node_type.type
            == "semantic_vad"
            for edge in self._plan.edges
        )

        async def recorded_chunks() -> AsyncIterator[bytes]:
            async for envelope in _single_input(input_queues[0]):
                chunk = cast(bytes, envelope.payload)
                audio.append(chunk)
                yield chunk

        async def emit_segment(
            start: int,
            end: int,
            *,
            evaluate_turn: bool,
        ) -> None:
            start = max(start, audio.base_sample)
            end = min(end, audio.end_sample)
            if end <= start or end - start < min_samples:
                return
            await router.emit(
                node_id,
                "segments",
                GraphAudioSegment(
                    segment=AudioSegment(
                        audio=audio.slice(start, end),
                        start_sample=start,
                        end_sample=end,
                        turn_completed=True,
                    ),
                    evaluate_turn=evaluate_turn,
                ),
            )

        async def emit_physical(start: int, end: int) -> None:
            current = max(start, audio.base_sample)
            final = min(end, audio.end_sample)
            while current + max_samples < final:
                cut = current + max_samples
                await emit_segment(
                    current,
                    cut,
                    evaluate_turn=True,
                )
                current = cut
            await emit_segment(current, final, evaluate_turn=True)

        async for update in engine.stream_detect(
            recorded_chunks(),
            sample_rate,
        ):
            if not isinstance(update, VadUpdate):
                raise TypeError(f"非法 VAD 更新类型: {type(update)!r}")
            last_processed = max(last_processed, update.processed_samples)
            processed = min(last_processed, audio.end_sample)
            for boundary in update.boundaries:
                if boundary.start_sample is not None:
                    start = min(
                        max(
                            boundary.start_sample - pre_roll_samples,
                            audio.base_sample,
                            0,
                        ),
                        audio.end_sample,
                    )
                    if active_start is None:
                        active_start = start
                        if self._include_vad_events:
                            await router.emit(
                                node_id,
                                "events",
                                SpeechStarted(
                                    start_sample=start,
                                    sample_rate=sample_rate,
                                ),
                            )
                    else:
                        active_start = min(active_start, start)
                if boundary.end_sample is not None:
                    end = min(
                        max(boundary.end_sample, 0),
                        audio.end_sample,
                    )
                    if active_start is not None and end > active_start:
                        if self._include_vad_events and not has_semantic_vad:
                            await router.emit(
                                node_id,
                                "events",
                                SpeechStopped(
                                    start_sample=active_start,
                                    end_sample=end,
                                    sample_rate=sample_rate,
                                ),
                            )
                        await emit_physical(active_start, end)
                    active_start = None

            if active_start is not None:
                while active_start + max_samples <= processed:
                    cut = active_start + max_samples
                    await emit_segment(
                        active_start,
                        cut,
                        evaluate_turn=False,
                    )
                    active_start = cut

            # VAD 可能在处理若干后续帧后才回报更早的语音起点。这里不能按
            # processed_time 主动裁剪；固定容量环形缓冲会自行淘汰过期音频。

        stream_end = audio.end_sample
        if active_start is not None and stream_end > active_start:
            if self._include_vad_events and not has_semantic_vad:
                await router.emit(
                    node_id,
                    "events",
                    SpeechStopped(
                        start_sample=active_start,
                        end_sample=stream_end,
                        sample_rate=sample_rate,
                    ),
                )
            await emit_physical(active_start, stream_end)

    def _max_segment_samples(self) -> int:
        physical = next(
            (
                node
                for node in self._plan.nodes.values()
                if node.node_type.type == "physical_vad"
            ),
            None,
        )
        if physical is None:
            raise RuntimeError("处理链缺少物理 VAD")
        config = cast(PhysicalVadNodeConfig, physical.config)
        return max(
            1,
            int(round(config.max_segment_seconds * self._options.sample_rate)),
        )

    async def _semantic_vad(
        self,
        node_id: str,
        node: CompiledNode,
        router: _EdgeRouter,
    ) -> None:
        input_queues = router.input_queues(node_id, "segments")
        if len(input_queues) != 1:
            raise RuntimeError("语义 VAD 必须有一个片段输入")
        engine = cast(AsyncVadEngine, self._engines[node_id])
        sample_rate = self._options.sample_rate
        config = cast(SemanticVadNodeConfig, node.config)
        max_samples = max(
            1,
            int(round(config.max_buffer_seconds * sample_rate)),
        )
        pending_audio = bytearray()
        pending_start: int | None = None

        async def emit_completed(
            start: int,
            audio: bytes,
        ) -> None:
            total_samples = len(audio) // 2
            if total_samples == 0:
                return
            await router.emit(
                node_id,
                "segments",
                GraphAudioSegment(
                    AudioSegment(
                        audio=audio,
                        start_sample=start,
                        end_sample=start + total_samples,
                        turn_completed=True,
                    ),
                    evaluate_turn=False,
                ),
            )

        async for envelope in _single_input(input_queues[0]):
            candidate = cast(GraphAudioSegment, envelope.payload)
            segment = candidate.segment
            candidate_samples = len(segment.audio) // 2

            if candidate_samples > max_samples:
                if pending_start is not None:
                    await emit_completed(pending_start, bytes(pending_audio))
                    pending_audio.clear()
                    pending_start = None
                offset = 0
                while offset < candidate_samples:
                    end = min(offset + max_samples, candidate_samples)
                    await emit_completed(
                        segment.start_sample + offset,
                        segment.audio[offset * 2 : end * 2],
                    )
                    offset = end
                continue

            if (
                pending_start is not None
                and len(pending_audio) // 2 + candidate_samples > max_samples
            ):
                await emit_completed(pending_start, bytes(pending_audio))
                pending_audio.clear()
                pending_start = None

            if pending_start is None:
                pending_start = segment.start_sample
            pending_audio.extend(segment.audio)

            completed = not candidate.evaluate_turn
            if candidate.evaluate_turn:
                try:
                    completed = await engine.detect_turn(
                        segment.audio,
                        sample_rate,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if node.failure_policy is FailurePolicy.FAIL_PIPELINE:
                        raise
                    logger.warning(
                        "语义 VAD 节点失败，按 fail_open 放行: node=%s",
                        node_id,
                        exc_info=True,
                    )
                    completed = True

            if completed:
                await emit_completed(pending_start, bytes(pending_audio))
                pending_audio.clear()
                pending_start = None
                continue

    async def _asr(
        self,
        node_id: str,
        router: _EdgeRouter,
    ) -> None:
        input_queues = router.input_queues(node_id, "segments")
        if len(input_queues) != 1:
            raise RuntimeError("ASR 必须有一个片段输入")
        engine = cast(AsyncAsrEngine, self._engines[node_id])
        async for envelope in _single_input(input_queues[0]):
            candidate = cast(GraphAudioSegment, envelope.payload)
            if (
                self._include_vad_events
                and self._plan.nodes[envelope.source_node].node_type.type
                == "semantic_vad"
            ):
                await router.emit(
                    node_id,
                    "events",
                    SpeechStopped(
                        start_sample=candidate.segment.start_sample,
                        end_sample=candidate.segment.end_sample,
                        sample_rate=self._options.sample_rate,
                    ),
                )
            async with self._asr_semaphore:
                async for result in engine.transcribe(
                    candidate.segment,
                    self._options,
                ):
                    await router.emit(node_id, "events", result)

    async def _session_output(
        self,
        node_id: str,
        router: _EdgeRouter,
    ) -> None:
        queues = router.input_queues(node_id, "events")
        async for envelope in _merged_input(queues):
            event = envelope.payload
            if not isinstance(
                event,
                (
                    TranscriptionResult,
                    SpeechStarted,
                    SpeechStopped,
                ),
            ):
                raise TypeError(f"非法会话事件类型: {type(event)!r}")
            await self._output.put(event)
