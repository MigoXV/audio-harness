from __future__ import annotations

import asyncio
import logging
import unicodedata
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, NamedTuple, cast

from audio_harness.errors import CapacityExceededError
from audio_harness.inferencers import Inferencer
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.media.pcm import PcmChunk
from audio_harness.workflows.compiler import (
    CompiledAudioGraph,
    CompiledAudioNode,
    PortKind,
)
from audio_harness.workflows.events import SpeechStarted, SpeechStopped
from audio_harness.workflows.models import (
    AsrNode,
    FailurePolicy,
    GraphInputRef,
    NodeOutputRef,
    PhysicalVadConfig,
    PhysicalVadNode,
    SemanticVadConfig,
    SemanticVadNode,
)

logger = logging.getLogger(__name__)

_EDGE_END = object()
_OUTPUT_END = object()


def _remove_punctuation(text: str) -> str:
    return "".join(
        character
        for character in text
        if not unicodedata.category(character).startswith("P")
    )


AudioOutputPayload = SpeechStarted | SpeechStopped | AsrInferencer.Result


@dataclass(frozen=True)
class AudioInferenceRequest:
    recognition: AsrInferencer.Options
    include_vad_events: bool = False
    include_vad_progress: bool = False
    include_interim_transcripts: bool = False


@dataclass(frozen=True)
class AudioRuntimeConfig:
    segment_queue_capacity: int
    result_queue_capacity: int

    def __post_init__(self) -> None:
        if self.segment_queue_capacity <= 0:
            raise ValueError("segment_queue_capacity 必须大于 0")
        if self.result_queue_capacity <= 0:
            raise ValueError("result_queue_capacity 必须大于 0")


@dataclass(frozen=True)
class AudioWorkflowOutput:
    name: str
    sequence: int
    payload: AudioOutputPayload


@dataclass(frozen=True)
class _Envelope:
    payload: object


class _SegmentCandidate(NamedTuple):
    audio: bytes
    start_sample: int
    end_sample: int
    turn_completed: bool = True
    evaluate_turn: bool = True


class AudioGraphExecutionError(RuntimeError):
    def __init__(self, node_id: str, cause: BaseException) -> None:
        self.node_id = node_id
        self.cause = cause
        super().__init__(f"语音节点 {node_id} 执行失败: {cause}")


class RollingPcmBuffer:
    """使用绝对采样下标访问的固定容量 PCM S16LE 环形缓冲。"""

    def __init__(self, capacity_samples: int) -> None:
        if capacity_samples <= 0:
            raise ValueError("音频环形缓冲容量必须大于 0")
        self._capacity_bytes = capacity_samples * 2
        self._storage = bytearray(self._capacity_bytes)
        self._start_byte = 0
        self._length_bytes = 0
        self._end_sample = 0

    @property
    def base_sample(self) -> int:
        return self._end_sample - self._length_bytes // 2

    @property
    def end_sample(self) -> int:
        return self._end_sample

    def append(self, audio: bytes) -> None:
        if len(audio) % 2:
            raise ValueError("PCM S16LE 音频字节数必须为偶数")
        if not audio:
            return
        input_bytes = len(audio)
        self._end_sample += input_bytes // 2
        if input_bytes >= self._capacity_bytes:
            self._storage[:] = audio[-self._capacity_bytes :]
            self._start_byte = 0
            self._length_bytes = self._capacity_bytes
            return
        overflow = max(0, self._length_bytes + input_bytes - self._capacity_bytes)
        if overflow:
            self._start_byte = (self._start_byte + overflow) % self._capacity_bytes
            self._length_bytes -= overflow
        write_at = (self._start_byte + self._length_bytes) % self._capacity_bytes
        first = min(input_bytes, self._capacity_bytes - write_at)
        self._storage[write_at : write_at + first] = audio[:first]
        remaining = input_bytes - first
        if remaining:
            self._storage[:remaining] = audio[first:]
        self._length_bytes += input_bytes

    def slice(self, start_sample: int, end_sample: int) -> bytes:
        if start_sample < self.base_sample or end_sample > self.end_sample:
            raise ValueError(
                "VAD 时间戳超出已记录音频范围: "
                f"[{start_sample}, {end_sample}) not in "
                f"[{self.base_sample}, {self.end_sample})"
            )
        if end_sample < start_sample:
            raise ValueError("音频切片结束位置不能早于开始位置")
        size = (end_sample - start_sample) * 2
        if size == 0:
            return b""
        offset = (start_sample - self.base_sample) * 2
        read_at = (self._start_byte + offset) % self._capacity_bytes
        first = min(size, self._capacity_bytes - read_at)
        if first == size:
            return bytes(self._storage[read_at : read_at + first])
        return bytes(self._storage[read_at:]) + bytes(self._storage[: size - first])


PhysicalEmission = tuple[str, _SegmentCandidate | SpeechStarted | SpeechStopped]


class PhysicalVadSession:
    """单个物理 VAD 节点的有界音频与边界状态。"""

    def __init__(self, config: PhysicalVadConfig, sample_rate: int) -> None:
        self._sample_rate = sample_rate
        self._min_samples = int(round(config.min_segment_seconds * sample_rate))
        self._max_samples = max(1, int(round(config.max_segment_seconds * sample_rate)))
        self._pre_roll_samples = int(round(config.speech_pre_roll_seconds * sample_rate))
        capacity = max(1, int(round(config.audio_buffer_seconds * sample_rate)))
        self._audio = RollingPcmBuffer(capacity)
        self._active_start: int | None = None
        self._last_processed = 0

    def append(self, chunk: PcmChunk) -> None:
        if chunk.start_sample != self._audio.end_sample:
            raise ValueError("物理 VAD 收到不连续的音频块")
        self._audio.append(chunk.pcm_s16le)

    def _segment(self, start: int, end: int, *, evaluate_turn: bool) -> PhysicalEmission | None:
        start = max(start, self._audio.base_sample)
        end = min(end, self._audio.end_sample)
        if end <= start or end - start < self._min_samples:
            return None
        return (
            "segments",
            _SegmentCandidate(
                audio=self._audio.slice(start, end),
                start_sample=start,
                end_sample=end,
                turn_completed=True,
                evaluate_turn=evaluate_turn,
            ),
        )

    def _physical_segments(self, start: int, end: int) -> list[PhysicalEmission]:
        result: list[PhysicalEmission] = []
        current = max(start, self._audio.base_sample)
        final = min(end, self._audio.end_sample)
        while current + self._max_samples < final:
            cut = current + self._max_samples
            item = self._segment(current, cut, evaluate_turn=True)
            if item is not None:
                result.append(item)
            current = cut
        item = self._segment(current, final, evaluate_turn=True)
        if item is not None:
            result.append(item)
        return result

    def update(self, update: VadInferencer.Update) -> list[PhysicalEmission]:
        result: list[PhysicalEmission] = []
        self._last_processed = max(self._last_processed, update.processed_samples)
        processed = min(self._last_processed, self._audio.end_sample)
        for boundary in update.boundaries:
            start_sample, end_sample = boundary
            if start_sample is not None:
                start = min(
                    max(
                        start_sample - self._pre_roll_samples,
                        self._audio.base_sample,
                        0,
                    ),
                    self._audio.end_sample,
                )
                if self._active_start is None:
                    self._active_start = start
                    result.append(
                        (
                            "speech_started",
                            SpeechStarted(start_sample=start, sample_rate=self._sample_rate),
                        )
                    )
                else:
                    self._active_start = min(self._active_start, start)
            if end_sample is not None:
                end = min(max(end_sample, 0), self._audio.end_sample)
                if self._active_start is not None and end > self._active_start:
                    start = self._active_start
                    result.extend(self._physical_segments(start, end))
                    result.append(
                        (
                            "speech_stopped",
                            SpeechStopped(
                                start_sample=start,
                                end_sample=end,
                                sample_rate=self._sample_rate,
                            ),
                        )
                    )
                self._active_start = None
        if self._active_start is not None:
            while self._active_start + self._max_samples <= processed:
                cut = self._active_start + self._max_samples
                item = self._segment(self._active_start, cut, evaluate_turn=False)
                if item is not None:
                    result.append(item)
                self._active_start = cut
        return result

    def finish(self) -> list[PhysicalEmission]:
        if self._active_start is None or self._audio.end_sample <= self._active_start:
            return []
        start = self._active_start
        end = self._audio.end_sample
        self._active_start = None
        return [
            *self._physical_segments(start, end),
            (
                "speech_stopped",
                SpeechStopped(start_sample=start, end_sample=end, sample_rate=self._sample_rate),
            ),
        ]


SemanticEmission = tuple[
    str,
    _SegmentCandidate | SpeechStopped | AsrInferencer.Result,
]


class SemanticVadSession:
    """单个语义 VAD 节点的轮次合并状态。"""

    def __init__(self, config: SemanticVadConfig, sample_rate: int) -> None:
        self._sample_rate = sample_rate
        self._max_samples = max(1, int(round(config.max_buffer_seconds * sample_rate)))
        self._pending_audio = bytearray()
        self._pending_start: int | None = None

    def _complete(self) -> list[SemanticEmission]:
        if self._pending_start is None or not self._pending_audio:
            self._pending_audio.clear()
            self._pending_start = None
            return []
        start = self._pending_start
        audio = bytes(self._pending_audio)
        end = start + len(audio) // 2
        self._pending_audio.clear()
        self._pending_start = None
        return [
            (
                "segments",
                _SegmentCandidate(
                    audio=audio,
                    start_sample=start,
                    end_sample=end,
                    turn_completed=True,
                    evaluate_turn=False,
                ),
            ),
            (
                "speech_stopped",
                SpeechStopped(start_sample=start, end_sample=end, sample_rate=self._sample_rate),
            ),
        ]

    async def accept(
        self,
        candidate: _SegmentCandidate,
        *,
        inferencer: VadInferencer,
        failure_policy: FailurePolicy,
    ) -> list[SemanticEmission]:
        samples = len(candidate.audio) // 2
        result: list[SemanticEmission] = []
        if samples > self._max_samples:
            result.extend(self._complete())
            offset = 0
            while offset < samples:
                end = min(offset + self._max_samples, samples)
                self._pending_start = candidate.start_sample + offset
                self._pending_audio.extend(candidate.audio[offset * 2 : end * 2])
                result.extend(self._complete())
                offset = end
            return result
        if (
            self._pending_start is not None
            and len(self._pending_audio) // 2 + samples > self._max_samples
        ):
            result.extend(self._complete())
        if self._pending_start is None:
            self._pending_start = candidate.start_sample
        self._pending_audio.extend(candidate.audio)
        completed = not candidate.evaluate_turn
        if candidate.evaluate_turn:
            try:
                completed = await inferencer.detect_turn(
                    candidate.audio,
                    self._sample_rate,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if failure_policy is FailurePolicy.FAIL_PIPELINE:
                    raise
                logger.warning("语义 VAD 失败，按 fail_open 放行", exc_info=True)
                completed = True
        if completed:
            result.extend(self._complete())
        else:
            result.append(
                (
                    "turn_progress",
                    AsrInferencer.Result(
                        transcript="",
                        is_final=True,
                        start_sample=candidate.start_sample,
                        end_sample=candidate.end_sample,
                        sample_rate=self._sample_rate,
                        turn_completed=False,
                    ),
                )
            )
        return result

    def finish(self) -> list[SemanticEmission]:
        return self._complete()


class _EdgeRouter:
    def __init__(
        self,
        plan: CompiledAudioGraph,
        *,
        config: AudioRuntimeConfig,
        output: asyncio.Queue[AudioWorkflowOutput | BaseException | object],
        active_nodes: set[str],
        active_outputs: set[str],
    ) -> None:
        self._output = output
        self._sequence = 0
        self._outgoing: dict[tuple[str, str], list[asyncio.Queue[object]]] = {}
        self._incoming: dict[tuple[str, str], asyncio.Queue[object]] = {}
        self._incoming_sources: dict[asyncio.Queue[object], str] = {}
        self._bindings: dict[tuple[str, str], list[str]] = {}
        self._closed: set[str] = set()
        for edge in plan.edges:
            if edge.target.node_id not in active_nodes:
                continue
            if (
                isinstance(edge.source, NodeOutputRef)
                and edge.source.node_id not in active_nodes
            ):
                continue
            if isinstance(edge.source, GraphInputRef):
                source_key = ("$input", edge.source.name)
                kind = PortKind.PCM_CHUNK
            else:
                source_key = (edge.source.node_id, edge.source.port)
                kind = plan.nodes[edge.source.node_id].spec.outputs[edge.source.port].kind
            capacity = (
                config.segment_queue_capacity
                if kind in {PortKind.PCM_CHUNK, PortKind.AUDIO_SEGMENT}
                else config.result_queue_capacity
            )
            queue: asyncio.Queue[object] = asyncio.Queue(maxsize=capacity)
            self._outgoing.setdefault(source_key, []).append(queue)
            self._incoming[(edge.target.node_id, edge.target.port)] = queue
            self._incoming_sources[queue] = source_key[0]
        for binding in plan.definition.outputs:
            if binding.name not in active_outputs:
                continue
            self._bindings.setdefault(
                (binding.source.node_id, binding.source.port), []
            ).append(binding.name)

    def input(self, node_id: str, port: str) -> asyncio.Queue[object]:
        try:
            return self._incoming[(node_id, port)]
        except KeyError as exc:
            raise RuntimeError(f"节点输入不存在: {node_id}.{port}") from exc

    async def emit(self, node_id: str, port: str, payload: object) -> None:
        for queue in self._outgoing.get((node_id, port), ()):
            await queue.put(_Envelope(payload))
        for name in self._bindings.get((node_id, port), ()):
            if not isinstance(
                payload,
                (SpeechStarted, SpeechStopped, AsrInferencer.Result),
            ):
                raise TypeError(f"出口 {name} 收到非法负载: {type(payload)!r}")
            await self._output.put(
                AudioWorkflowOutput(name=name, sequence=self._sequence, payload=payload)
            )
            self._sequence += 1

    async def close(self, node_id: str, *, discard: bool = False) -> None:
        if node_id in self._closed:
            return
        self._closed.add(node_id)
        queues: set[asyncio.Queue[object]] = set()
        for (source_node, _), values in self._outgoing.items():
            if source_node == node_id:
                queues.update(values)
        for queue in queues:
            if discard:
                while not queue.empty():
                    queue.get_nowait()
            await queue.put(_EDGE_END)

    async def drain_inputs(self, node_id: str) -> None:
        queues = [
            queue
            for (target_node, _), queue in self._incoming.items()
            if target_node == node_id
        ]
        await asyncio.gather(
            *(
                _drain(
                    queue,
                    source_node=self._incoming_sources[queue],
                    closed_nodes=self._closed,
                )
                for queue in queues
            )
        )


async def _items(queue: asyncio.Queue[object]) -> AsyncIterator[object]:
    while True:
        item = await queue.get()
        if item is _EDGE_END:
            return
        assert isinstance(item, _Envelope)
        yield item.payload


async def _drain(
    queue: asyncio.Queue[object],
    *,
    source_node: str,
    closed_nodes: set[str],
) -> None:
    while True:
        if queue.empty() and source_node in closed_nodes:
            return
        item = await queue.get()
        if item is _EDGE_END:
            return


def _output_ancestors(
    plan: CompiledAudioGraph,
    output_names: set[str],
) -> set[str]:
    reverse: dict[str, set[str]] = {node_id: set() for node_id in plan.nodes}
    for edge in plan.edges:
        if isinstance(edge.source, NodeOutputRef):
            reverse[edge.target.node_id].add(edge.source.node_id)
    pending = [
        binding.source.node_id
        for binding in plan.definition.outputs
        if binding.name in output_names
    ]
    ancestors: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in ancestors:
            continue
        ancestors.add(node_id)
        pending.extend(reverse[node_id])
    return ancestors


class AudioWorkflowRuntime:
    """单次会话内的有状态语音 DAG 执行器。"""

    def __init__(
        self,
        *,
        plan: CompiledAudioGraph,
        inferencers: Mapping[str, Inferencer],
        runtime_config: AudioRuntimeConfig,
        asr_semaphore: asyncio.Semaphore,
    ) -> None:
        self._plan = plan
        expected = set(plan.nodes)
        actual = set(inferencers)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(
                f"节点 Inferencer 绑定不完整: missing={missing}, extra={extra}"
            )
        self._inferencers = dict(inferencers)
        self._vad_inferencers: dict[str, VadInferencer] = {}
        self._asr_inferencers: dict[str, AsrInferencer] = {}
        for node_id, node in plan.nodes.items():
            inferencer = self._inferencers[node_id]
            if node.spec.kind in {"physical_vad", "semantic_vad"}:
                if not isinstance(inferencer, VadInferencer):
                    raise TypeError(
                        f"节点 {node_id} 需要 VadInferencer，实际为 "
                        f"{type(inferencer).__name__}"
                    )
                self._vad_inferencers[node_id] = inferencer
            elif node.spec.kind == "asr":
                if not isinstance(inferencer, AsrInferencer):
                    raise TypeError(
                        f"节点 {node_id} 需要 AsrInferencer，实际为 "
                        f"{type(inferencer).__name__}"
                    )
                self._asr_inferencers[node_id] = inferencer
        self._runtime_config = runtime_config
        self._asr_semaphore = asr_semaphore

    async def stream(
        self,
        audio: AsyncIterator[PcmChunk],
        request: AudioInferenceRequest,
    ) -> AsyncIterator[AudioWorkflowOutput]:
        output: asyncio.Queue[AudioWorkflowOutput | BaseException | object] = asyncio.Queue(
            maxsize=self._runtime_config.result_queue_capacity
        )
        available_outputs = {
            binding.name for binding in self._plan.definition.outputs
        }
        active_outputs = available_outputs.difference({"interim_transcript"})
        if request.include_interim_transcripts:
            active_outputs.add("interim_transcript")
        active_nodes = _output_ancestors(self._plan, active_outputs)
        protected_nodes = _output_ancestors(
            self._plan,
            available_outputs.difference({"interim_transcript"}),
        )
        preview_nodes = (
            _output_ancestors(self._plan, {"interim_transcript"})
            .difference(protected_nodes)
        )
        router = _EdgeRouter(
            self._plan,
            config=self._runtime_config,
            output=output,
            active_nodes=active_nodes,
            active_outputs=active_outputs,
        )
        tasks = [
            asyncio.create_task(
                self._feed(audio, request, router),
                name="audio-graph-input",
            ),
            *[
                asyncio.create_task(
                    self._run_node(
                        node_id,
                        self._plan.nodes[node_id],
                        request,
                        router,
                        best_effort=node_id in preview_nodes,
                    ),
                    name=f"audio-graph-node-{node_id}",
                )
                for node_id in self._plan.topological_order
                if node_id in active_nodes
            ],
        ]
        supervisor = asyncio.create_task(
            self._supervise(tasks, output),
            name="audio-graph-supervisor",
        )
        try:
            while True:
                item = await output.get()
                if item is _OUTPUT_END:
                    return
                if isinstance(item, BaseException):
                    raise item
                assert isinstance(item, AudioWorkflowOutput)
                yield item
        finally:
            if not supervisor.done():
                supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)

    async def _supervise(
        self,
        tasks: list[asyncio.Task[None]],
        output: asyncio.Queue[AudioWorkflowOutput | BaseException | object],
    ) -> None:
        failure: BaseException | None = None
        cancelled = False
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    failure = task.exception()
                    break
            if failure is not None:
                for task in pending:
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            cancelled = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            if not cancelled:
                if failure is not None:
                    await output.put(failure)
                await output.put(_OUTPUT_END)

    async def _feed(
        self,
        audio: AsyncIterator[PcmChunk],
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        failed = False
        expected_start = 0
        try:
            async for chunk in audio:
                if chunk.sample_rate != request.recognition.sample_rate:
                    raise ValueError("音频块采样率与识别请求不一致")
                if len(chunk.pcm_s16le) % 2:
                    raise ValueError("PCM S16LE 音频字节数必须为偶数")
                if chunk.start_sample != expected_start:
                    raise ValueError("音频块采样位置必须连续且从 0 开始")
                expected_start = chunk.end_sample
                if chunk.pcm_s16le:
                    await router.emit("$input", "audio", chunk)
        except asyncio.CancelledError:
            failed = True
            raise
        except BaseException as exc:
            failed = True
            raise AudioGraphExecutionError("$input", exc) from exc
        finally:
            await router.close("$input", discard=failed)

    async def _run_node(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
        *,
        best_effort: bool,
    ) -> None:
        failed = False
        try:
            if node.spec.kind == "physical_vad":
                await self._physical_vad(node_id, node, request, router)
            elif node.spec.kind == "semantic_vad":
                await self._semantic_vad(node_id, node, request, router)
            elif node.spec.kind == "asr":
                await self._asr(
                    node_id,
                    node,
                    request,
                    router,
                    best_effort=best_effort,
                )
            else:
                raise RuntimeError(f"没有节点执行器: {node.spec.kind}")
        except asyncio.CancelledError:
            failed = True
            raise
        except BaseException as exc:
            if best_effort:
                logger.warning(
                    "预览语音节点失败，正式转写继续: %s",
                    node_id,
                    exc_info=True,
                )
                await router.close(node_id, discard=True)
                await router.drain_inputs(node_id)
                return
            failed = True
            raise AudioGraphExecutionError(node_id, exc) from exc
        finally:
            await router.close(node_id, discard=failed)

    async def _physical_vad(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        definition = cast(PhysicalVadNode, node.definition)
        state = PhysicalVadSession(definition.config, request.recognition.sample_rate)
        inferencer = self._vad_inferencers[node_id]
        queue = router.input(node_id, "audio")

        async def recorded() -> AsyncIterator[bytes]:
            async for item in _items(queue):
                chunk = cast(PcmChunk, item)
                state.append(chunk)
                yield chunk.pcm_s16le

        async def publish(values: list[PhysicalEmission]) -> None:
            for port, payload in values:
                if port.startswith("speech_") and not request.include_vad_events:
                    continue
                await router.emit(node_id, port, payload)

        postprocessing = definition.config.postprocessing
        request_postprocessing = (
            VadInferencer.Postprocessing(
                processor=postprocessing.processor,
                version=postprocessing.version,
                parameters=postprocessing.parameters.model_dump(),
            )
            if postprocessing is not None
            else None
        )
        updates = (
            inferencer.stream_detect(
                recorded(),
                request.recognition.sample_rate,
                postprocessing=request_postprocessing,
            )
            if request_postprocessing is not None
            else inferencer.stream_detect(
                recorded(),
                request.recognition.sample_rate,
            )
        )
        async for update in updates:
            if not isinstance(update, VadInferencer.Update):
                raise TypeError(f"非法 VAD 更新类型: {type(update)!r}")
            await publish(state.update(update))
        await publish(state.finish())

    async def _semantic_vad(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        definition = cast(SemanticVadNode, node.definition)
        state = SemanticVadSession(definition.config, request.recognition.sample_rate)
        inferencer = self._vad_inferencers[node_id]
        async for item in _items(router.input(node_id, "segments")):
            candidate = cast(_SegmentCandidate, item)
            values = await state.accept(
                candidate,
                inferencer=inferencer,
                failure_policy=definition.failure_policy,
            )
            for port, payload in values:
                if port == "speech_stopped" and not request.include_vad_events:
                    continue
                if port == "turn_progress" and not request.include_vad_progress:
                    continue
                await router.emit(node_id, port, payload)
        for port, payload in state.finish():
            if port == "speech_stopped" and not request.include_vad_events:
                continue
            await router.emit(node_id, port, payload)

    async def _asr(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
        *,
        best_effort: bool,
    ) -> None:
        definition = cast(AsrNode, node.definition)
        inferencer = self._asr_inferencers[node_id]
        async for item in _items(router.input(node_id, "segments")):
            candidate = cast(_SegmentCandidate, item)
            try:
                async with self._asr_slot(best_effort=best_effort):
                    async for result in inferencer.transcribe(
                        candidate.audio,
                        start_sample=candidate.start_sample,
                        end_sample=candidate.end_sample,
                        turn_completed=candidate.turn_completed,
                        options=request.recognition,
                    ):
                        if definition.config.remove_punctuation:
                            result = result._replace(
                                transcript=_remove_punctuation(result.transcript),
                            )
                        await router.emit(node_id, "transcripts", result)
            except CapacityExceededError:
                logger.debug("ASR 容量已满，跳过预览音频块: %s", node_id)

    @asynccontextmanager
    async def _asr_slot(self, *, best_effort: bool) -> AsyncIterator[None]:
        limiter = self._asr_semaphore
        if best_effort and hasattr(limiter, "try_slot"):
            async with cast(Any, limiter).try_slot():
                yield
            return
        if best_effort and hasattr(limiter, "locked") and limiter.locked():
            raise CapacityExceededError("ASR 并发容量已满")
        async with limiter:
            yield
