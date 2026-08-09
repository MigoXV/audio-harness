from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

from audio_harness.domain import (
    AsyncAsrEngine,
    AsyncVadEngine,
    AudioSegment,
    PcmChunk,
    RecognitionOptions,
    SpeechStarted,
    SpeechStopped,
    TranscriptionResult,
    VadUpdate,
)
from audio_harness.inferencers.contracts import ManagedInferencer
from audio_harness.workflows.compiler import (
    CompiledAudioGraph,
    CompiledAudioNode,
    PortKind,
)
from audio_harness.workflows.models import (
    FailurePolicy,
    GraphInputRef,
    PhysicalVadConfig,
    PhysicalVadNode,
    SemanticVadConfig,
    SemanticVadNode,
)

logger = logging.getLogger(__name__)

_EDGE_END = object()
_OUTPUT_END = object()

AudioOutputPayload = SpeechStarted | SpeechStopped | TranscriptionResult


@dataclass(frozen=True)
class AudioInferenceRequest:
    recognition: RecognitionOptions
    include_vad_events: bool = False
    include_vad_progress: bool = False


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


@dataclass(frozen=True)
class _SegmentCandidate:
    segment: AudioSegment
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
                AudioSegment(
                    audio=self._audio.slice(start, end),
                    start_sample=start,
                    end_sample=end,
                    turn_completed=True,
                ),
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

    def update(self, update: VadUpdate) -> list[PhysicalEmission]:
        result: list[PhysicalEmission] = []
        self._last_processed = max(self._last_processed, update.processed_samples)
        processed = min(self._last_processed, self._audio.end_sample)
        for boundary in update.boundaries:
            if boundary.start_sample is not None:
                start = min(
                    max(
                        boundary.start_sample - self._pre_roll_samples,
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
            if boundary.end_sample is not None:
                end = min(max(boundary.end_sample, 0), self._audio.end_sample)
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


SemanticEmission = tuple[str, _SegmentCandidate | SpeechStopped | TranscriptionResult]


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
                    AudioSegment(
                        audio=audio,
                        start_sample=start,
                        end_sample=end,
                        turn_completed=True,
                    ),
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
        engine: AsyncVadEngine,
        failure_policy: FailurePolicy,
    ) -> list[SemanticEmission]:
        segment = candidate.segment
        samples = len(segment.audio) // 2
        result: list[SemanticEmission] = []
        if samples > self._max_samples:
            result.extend(self._complete())
            offset = 0
            while offset < samples:
                end = min(offset + self._max_samples, samples)
                self._pending_start = segment.start_sample + offset
                self._pending_audio.extend(segment.audio[offset * 2 : end * 2])
                result.extend(self._complete())
                offset = end
            return result
        if (
            self._pending_start is not None
            and len(self._pending_audio) // 2 + samples > self._max_samples
        ):
            result.extend(self._complete())
        if self._pending_start is None:
            self._pending_start = segment.start_sample
        self._pending_audio.extend(segment.audio)
        completed = not candidate.evaluate_turn
        if candidate.evaluate_turn:
            try:
                completed = await engine.detect_turn(segment.audio, self._sample_rate)
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
                    TranscriptionResult(
                        transcript="",
                        is_final=True,
                        start_sample=segment.start_sample,
                        end_sample=segment.end_sample,
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
    ) -> None:
        self._output = output
        self._sequence = 0
        self._outgoing: dict[tuple[str, str], list[asyncio.Queue[object]]] = {}
        self._incoming: dict[tuple[str, str], asyncio.Queue[object]] = {}
        self._bindings: dict[tuple[str, str], list[str]] = {}
        for edge in plan.edges:
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
        for binding in plan.definition.outputs:
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
            if not isinstance(payload, (SpeechStarted, SpeechStopped, TranscriptionResult)):
                raise TypeError(f"出口 {name} 收到非法负载: {type(payload)!r}")
            await self._output.put(
                AudioWorkflowOutput(name=name, sequence=self._sequence, payload=payload)
            )
            self._sequence += 1

    async def close(self, node_id: str, *, discard: bool = False) -> None:
        queues: set[asyncio.Queue[object]] = set()
        for (source_node, _), values in self._outgoing.items():
            if source_node == node_id:
                queues.update(values)
        for queue in queues:
            if discard:
                while not queue.empty():
                    queue.get_nowait()
            await queue.put(_EDGE_END)


async def _items(queue: asyncio.Queue[object]) -> AsyncIterator[object]:
    while True:
        item = await queue.get()
        if item is _EDGE_END:
            return
        assert isinstance(item, _Envelope)
        yield item.payload


class AudioWorkflowInferencer:
    """协议无关的类型化语音 DAG 推理门面。"""

    def __init__(
        self,
        *,
        plan: CompiledAudioGraph,
        engines: dict[str, ManagedInferencer],
        runtime_config: AudioRuntimeConfig,
        asr_semaphore: asyncio.Semaphore,
    ) -> None:
        self._plan = plan
        self._engines = engines
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
        router = _EdgeRouter(self._plan, config=self._runtime_config, output=output)
        tasks = [
            asyncio.create_task(
                self._feed(audio, request, router),
                name="audio-graph-input",
            ),
            *[
                asyncio.create_task(
                    self._run_node(node_id, self._plan.nodes[node_id], request, router),
                    name=f"audio-graph-node-{node_id}",
                )
                for node_id in self._plan.topological_order
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
    ) -> None:
        failed = False
        try:
            if node.spec.kind == "physical_vad":
                await self._physical_vad(node_id, node, request, router)
            elif node.spec.kind == "semantic_vad":
                await self._semantic_vad(node_id, node, request, router)
            elif node.spec.kind == "asr":
                await self._asr(node_id, request, router)
            else:
                raise RuntimeError(f"没有节点执行器: {node.spec.kind}")
        except asyncio.CancelledError:
            failed = True
            raise
        except BaseException as exc:
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
        engine = cast(AsyncVadEngine, self._engines[node_id])
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

        async for update in engine.stream_detect(recorded(), request.recognition.sample_rate):
            if not isinstance(update, VadUpdate):
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
        engine = cast(AsyncVadEngine, self._engines[node_id])
        async for item in _items(router.input(node_id, "segments")):
            candidate = cast(_SegmentCandidate, item)
            values = await state.accept(
                candidate,
                engine=engine,
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
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        engine = cast(AsyncAsrEngine, self._engines[node_id])
        async for item in _items(router.input(node_id, "segments")):
            candidate = cast(_SegmentCandidate, item)
            async with self._asr_semaphore:
                async for result in engine.transcribe(candidate.segment, request.recognition):
                    await router.emit(node_id, "transcripts", result)
