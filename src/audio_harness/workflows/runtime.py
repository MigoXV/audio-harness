from __future__ import annotations

import asyncio
import logging
import unicodedata
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from audio_harness.errors import CapacityExceededError
from audio_harness.inferencers import Inferencer
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.inferencers.embedding.base import EngineFeatureInferencer
from audio_harness.inferencers.diarization import DiarizationInferencer
from audio_harness.inferencers.denoise import DenoiseInferencer
from audio_harness.media.agc import RealtimeAutomaticGainControl
from audio_harness.media.pcm import PcmChunk, VadAudioChunk, mix_pcm_s16le
from audio_harness.workflows.compiler import (
    CompiledAudioGraph,
    CompiledAudioNode,
    PortKind,
    speech_started_physical_vad,
)
from audio_harness.workflows.events import (
    AudioEmbeddingResult,
    DiarizationClusterResult,
    DiarizationLocalResult,
    SpeakerFilterResult,
    SpeakerTranscriptionResult,
    SpeechStarted,
    SpeechStopped,
    VadProgress,
    VectorSearchResult,
)
from audio_harness.workflows.models import (
    AutomaticGainControlNode,
    AsrNode,
    FailurePolicy,
    GraphInputRef,
    NodeOutputRef,
    PhysicalVadNode,
    SpeakerDiarizationNode,
    SpeakerClusteringNode,
    SemanticVadNode,
    AudioEmbeddingNode,
    SpeakerFilterNode,
    StreamingDenoiseNode,
    VectorSearchNode,
)
from audio_harness.sessions.contracts import (
    SegmentCandidate as _SegmentCandidate,
    candidate_physical_segment_ids,
)
from audio_harness.sessions.diarization import DiarizationSession
from audio_harness.sessions.physical_vad import PhysicalVadSession
from audio_harness.sessions.semantic_vad import SemanticVadSession
from audio_harness.sessions.speaker_clustering import SpeakerClusteringSession
from audio_harness.vectors.service import VectorService

logger = logging.getLogger(__name__)

_EDGE_END = object()
_OUTPUT_END = object()


def _remove_punctuation(text: str) -> str:
    return "".join(
        character
        for character in text
        if not unicodedata.category(character).startswith("P")
    )


def _join_transcript_parts(parts: list[str]) -> str:
    result = ""
    for raw in parts:
        part = raw.strip()
        if not part:
            continue
        separator = (
            " "
            if result
            and result[-1].isascii()
            and result[-1].isalnum()
            and part[0].isascii()
            and part[0].isalnum()
            else ""
        )
        result += separator + part
    return result


AudioOutputPayload = (
    SpeechStarted
    | SpeechStopped
    | VadProgress
    | AsrInferencer.Result
    | VectorSearchResult
    | SpeakerFilterResult
    | DiarizationLocalResult
    | SpeakerTranscriptionResult
    | DiarizationClusterResult
)


@dataclass(frozen=True)
class AudioInferenceRequest:
    recognition: AsrInferencer.Options
    include_vad_events: bool = False
    include_physical_vad_events: bool = False
    include_vad_progress: bool = False
    include_interim_transcripts: bool = False
    include_vector_search: bool = False
    include_speaker_filter: bool = False
    include_diarization: bool = False


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


@dataclass
class _SourceTranscriptState:
    start_sample: int
    end_sample: int
    turn_completed: bool
    parts: list[tuple[int, str]] = field(default_factory=list)
    language_code: str | None = None


class AudioGraphExecutionError(RuntimeError):
    def __init__(self, node_id: str, cause: BaseException) -> None:
        self.node_id = node_id
        self.cause = cause
        super().__init__(f"语音节点 {node_id} 执行失败: {cause}")


class _EdgeRouter:
    def __init__(
        self,
        plan: CompiledAudioGraph,
        *,
        config: AudioRuntimeConfig,
        output: asyncio.Queue[AudioWorkflowOutput | BaseException | object],
        active_nodes: set[str],
        active_outputs: set[str],
        implicit_bindings: Mapping[tuple[str, str], tuple[str, ...]] | None = None,
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
                if kind
                in {
                    PortKind.PCM_CHUNK,
                    PortKind.VAD_AUDIO_CHUNK,
                    PortKind.AUDIO_SEGMENT,
                    PortKind.DIARIZATION_SEGMENT,
                }
                else config.result_queue_capacity
            )
            queue: asyncio.Queue[object] = asyncio.Queue(maxsize=capacity)
            self._outgoing.setdefault(source_key, []).append(queue)
            self._incoming[(edge.target.node_id, edge.target.port)] = queue
            self._incoming_sources[queue] = source_key[0]
        for source, names in (implicit_bindings or {}).items():
            self._bindings.setdefault(source, []).extend(names)
        for binding in plan.definition.outputs:
            if binding.name not in active_outputs:
                continue
            self._bindings.setdefault(
                (binding.source.node_id, binding.source.port), []
            ).append(binding.name)

    def has_consumers(self, node_id: str, port: str) -> bool:
        source = (node_id, port)
        return bool(self._outgoing.get(source) or self._bindings.get(source))

    def input(self, node_id: str, port: str) -> asyncio.Queue[object]:
        try:
            return self._incoming[(node_id, port)]
        except KeyError as exc:
            raise RuntimeError(f"节点输入不存在: {node_id}.{port}") from exc

    def has_input(self, node_id: str, port: str) -> bool:
        return (node_id, port) in self._incoming

    async def emit(self, node_id: str, port: str, payload: object) -> None:
        for queue in self._outgoing.get((node_id, port), ()):
            await queue.put(_Envelope(payload))
        for name in self._bindings.get((node_id, port), ()):
            if not isinstance(
                payload,
                (
                    SpeechStarted,
                    SpeechStopped,
                    VadProgress,
                    AsrInferencer.Result,
                    VectorSearchResult,
                    SpeakerFilterResult,
                    DiarizationLocalResult,
                    SpeakerTranscriptionResult,
                    DiarizationClusterResult,
                ),
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
        embedding_semaphore: asyncio.Semaphore | None = None,
        vectors: VectorService | None = None,
    ) -> None:
        self._plan = plan
        expected = {node_id for node_id, node in plan.nodes.items() if node.engine_snapshot is not None}
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
        self._engine_feature_inferencers: dict[
            str,
            EngineFeatureInferencer,
        ] = {}
        self._diarization_inferencers: dict[str, DiarizationInferencer] = {}
        self._denoise_inferencers: dict[str, DenoiseInferencer] = {}
        for node_id, node in plan.nodes.items():
            if node.engine_snapshot is None:
                continue
            inferencer = self._inferencers[node_id]
            if node.spec.kind == "streaming_denoise":
                if not isinstance(inferencer, DenoiseInferencer):
                    raise TypeError(
                        f"节点 {node_id} 需要 DenoiseInferencer，实际为 "
                        f"{type(inferencer).__name__}"
                    )
                self._denoise_inferencers[node_id] = inferencer
            elif node.spec.kind in {"physical_vad", "semantic_vad"}:
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
            elif node.spec.kind in {"audio_embedding", "speaker_clustering"}:
                if not isinstance(inferencer, EngineFeatureInferencer):
                    raise TypeError(
                        f"节点 {node_id} 需要 EngineFeatureInferencer"
                    )
                self._engine_feature_inferencers[node_id] = inferencer
            elif node.spec.kind == "speaker_diarization":
                if not isinstance(inferencer, DiarizationInferencer):
                    raise TypeError(
                        f"节点 {node_id} 需要 DiarizationInferencer"
                    )
                self._diarization_inferencers[node_id] = inferencer
        self._runtime_config = runtime_config
        self._asr_semaphore = asr_semaphore
        self._embedding_semaphore = embedding_semaphore
        self._vectors = vectors

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
        active_outputs = available_outputs.difference(
            {
                "interim_transcript",
                "vector_search",
                "speaker_filter",
                "diarization_local",
                "speaker_transcript",
                "diarization_cluster",
            }
        )
        if request.include_interim_transcripts:
            active_outputs.add("interim_transcript")
        if request.include_vector_search:
            active_outputs.add("vector_search")
        if request.include_speaker_filter:
            active_outputs.add("speaker_filter")
        if request.include_diarization:
            active_outputs.add("diarization_local")
            active_outputs.add("speaker_transcript")
            active_outputs.add("diarization_cluster")
        active_nodes = _output_ancestors(self._plan, active_outputs)
        implicit_bindings: dict[tuple[str, str], tuple[str, ...]] = {}
        if request.include_physical_vad_events:
            physical_vad = speech_started_physical_vad(self._plan)
            if physical_vad is not None:
                implicit_bindings[(physical_vad[0], "speech_started")] = (
                    "physical_speech_started",
                )
                implicit_bindings[(physical_vad[0], "speech_stopped")] = (
                    "physical_speech_stopped",
                )
        if request.include_vad_progress:
            physical_vad = speech_started_physical_vad(self._plan)
            if (
                physical_vad is not None
                and physical_vad[1].config.progress_interval_seconds > 0
            ):
                implicit_bindings[(physical_vad[0], "progress")] = (
                    "vad_progress",
                )
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
            implicit_bindings=implicit_bindings,
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
            if node.spec.kind == "automatic_gain_control":
                await self._automatic_gain_control(node_id, node, request, router)
            elif node.spec.kind == "streaming_denoise":
                await self._streaming_denoise(node_id, node, request, router)
            elif node.spec.kind == "physical_vad":
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
            elif node.spec.kind == "audio_embedding":
                await self._audio_embedding(node_id, node, request, router)
            elif node.spec.kind == "speaker_diarization":
                await self._speaker_diarization(
                    node_id,
                    node,
                    request,
                    router,
                )
            elif node.spec.kind == "speaker_clustering":
                await self._speaker_clustering(
                    node_id,
                    node,
                    request,
                    router,
                )
            elif node.spec.kind == "vector_search":
                await self._vector_search(node_id, node, router)
            elif node.spec.kind == "speaker_filter":
                await self._speaker_filter(node_id, node, request, router)
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

    async def _automatic_gain_control(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        definition = cast(AutomaticGainControlNode, node.definition)
        config = definition.config
        processor = RealtimeAutomaticGainControl(
            sample_rate=request.recognition.sample_rate,
            attack_seconds=config.attack_seconds,
            decay_seconds=config.decay_seconds,
            soft_knee_db=config.soft_knee_db,
            initial_level_db=config.initial_level_db,
            output_gain_db=config.output_gain_db,
        )
        try:
            async for item in _items(router.input(node_id, "audio")):
                chunk = cast(PcmChunk, item)
                if chunk.sample_rate != request.recognition.sample_rate:
                    raise ValueError("AGC 音频块采样率与识别请求不一致")
                await router.emit(
                    node_id,
                    "audio",
                    PcmChunk(
                        pcm_s16le=processor.process(chunk.pcm_s16le),
                        sample_rate=chunk.sample_rate,
                        start_sample=chunk.start_sample,
                    ),
                )
        finally:
            processor.close()

    async def _streaming_denoise(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        definition = cast(StreamingDenoiseNode, node.definition)
        inferencer = self._denoise_inferencers[node_id]
        queue = router.input(node_id, "audio")
        pending_original = bytearray()
        input_drained = False
        emitted_samples = 0
        sample_rate = request.recognition.sample_rate

        async def recorded() -> AsyncIterator[bytes]:
            nonlocal input_drained
            async for item in _items(queue):
                chunk = cast(PcmChunk, item)
                pending_original.extend(chunk.pcm_s16le)
                yield chunk.pcm_s16le
            input_drained = True

        async def emit_pair(original: bytes, denoised: bytes) -> None:
            nonlocal emitted_samples
            weight = definition.config.denoised_weight
            vad_audio = (
                denoised
                if weight is None
                else mix_pcm_s16le(original, denoised, weight)
            )
            await router.emit(
                node_id,
                "audio",
                VadAudioChunk(
                    original_pcm_s16le=original,
                    vad_pcm_s16le=vad_audio,
                    sample_rate=sample_rate,
                    start_sample=emitted_samples,
                ),
            )
            emitted_samples += len(original) // 2

        async def emit_bypass(original: bytes) -> None:
            if original:
                await emit_pair(original, original)

        try:
            async for synchronized in inferencer.stream_synchronized(
                recorded(),
                sample_rate,
            ):
                original = synchronized.original_pcm_s16le
                size = len(original)
                if size > len(pending_original):
                    raise ValueError("同步降噪回传的原音超过本地待确认音频")
                if bytes(pending_original[:size]) != original:
                    raise ValueError("同步降噪回传的原音与输入不一致")
                del pending_original[:size]
                await emit_pair(original, synchronized.denoised_pcm_s16le)
            if not input_drained or pending_original:
                raise ValueError("同步降噪在输入完全确认前结束")
        except asyncio.CancelledError:
            raise
        except Exception:
            if definition.failure_policy is FailurePolicy.FAIL_PIPELINE:
                raise
            logger.warning("在线降噪失败，按 fail_open 旁路原音", exc_info=True)
            await emit_bypass(bytes(pending_original))
            pending_original.clear()
            if not input_drained:
                async for item in _items(queue):
                    chunk = cast(PcmChunk, item)
                    await emit_bypass(chunk.pcm_s16le)

    async def _physical_vad(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        definition = cast(PhysicalVadNode, node.definition)
        session = PhysicalVadSession(
            definition.config,
            request.recognition.sample_rate,
            self._vad_inferencers[node_id],
        )

        input_port = (
            "detection_audio"
            if router.has_input(node_id, "detection_audio")
            else "audio"
        )

        async def audio():
            async for item in _items(router.input(node_id, input_port)):
                yield cast(PcmChunk | VadAudioChunk, item)

        async for port, payload in session.run(
            audio(),
            progress_enabled=router.has_consumers(node_id, "progress"),
        ):
            if port.startswith("speech_") and not request.include_vad_events:
                continue
            await router.emit(node_id, port, payload)

    async def _semantic_vad(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        definition = cast(SemanticVadNode, node.definition)
        session = SemanticVadSession(
            definition.config,
            request.recognition.sample_rate,
            self._vad_inferencers[node_id],
        )

        async def candidates():
            async for item in _items(router.input(node_id, "segments")):
                yield cast(_SegmentCandidate, item)

        async for port, payload in session.run(
            candidates(),
            failure_policy=definition.failure_policy,
        ):
            if port == "speech_stopped" and not request.include_vad_events:
                continue
            if port == "turn_progress" and not request.include_vad_progress:
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
        source_states: dict[str, _SourceTranscriptState] = {}
        async for item in _items(router.input(node_id, "segments")):
            candidate = cast(_SegmentCandidate, item)
            context = candidate.diarization
            if context is not None:
                state = source_states.setdefault(
                    context.item_source_id,
                    _SourceTranscriptState(
                        start_sample=context.source_start_sample,
                        end_sample=context.source_end_sample,
                        turn_completed=True,
                    ),
                )
                state.start_sample = min(
                    state.start_sample,
                    context.source_start_sample,
                )
                state.end_sample = max(
                    state.end_sample,
                    context.source_end_sample,
                )
                # diarization 会把同一语义轮次内、被静音隔开的 PCM 拼接后
                # 一次送入 ASR；最短长度应按实际语音而非会话墙钟跨度计算。
                segment_samples = len(candidate.audio) // 2
                minimum_samples = (
                    context.min_asr_segment_seconds
                    * request.recognition.sample_rate
                )
                if segment_samples < minimum_samples:
                    if context.source_completed:
                        ordered = [
                            text
                            for _, text in sorted(
                                state.parts,
                                key=lambda value: value[0],
                            )
                        ]
                        await router.emit(
                            node_id,
                            "transcripts",
                            AsrInferencer.Result(
                                transcript=_join_transcript_parts(ordered),
                                is_final=True,
                                start_sample=state.start_sample,
                                end_sample=state.end_sample,
                                sample_rate=request.recognition.sample_rate,
                                turn_completed=state.turn_completed,
                                language_code=state.language_code,
                            ),
                        )
                        del source_states[context.item_source_id]
                    continue
                sent_transcript = ""
                final_transcript: str | None = None
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
                                    transcript=_remove_punctuation(
                                        result.transcript
                                    ),
                                )
                            state.language_code = (
                                result.language_code or state.language_code
                            )
                            if result.is_final:
                                final_transcript = result.transcript
                                await router.emit(
                                    node_id,
                                    "speaker_transcripts",
                                    SpeakerTranscriptionResult(
                                        phase="completed",
                                        item_source_id=context.item_source_id,
                                        segment_id=context.segment_id,
                                        batch_id=context.batch_id,
                                        local_speaker_id=(
                                            context.local_speaker_id
                                        ),
                                        speaker=context.speaker,
                                        source_start_sample=(
                                            context.source_start_sample
                                        ),
                                        source_end_sample=(
                                            context.source_end_sample
                                        ),
                                        start_sample=candidate.start_sample,
                                        end_sample=candidate.end_sample,
                                        sample_rate=result.sample_rate,
                                        text=result.transcript,
                                        language_code=result.language_code,
                                    ),
                                )
                            else:
                                delta = (
                                    result.transcript[len(sent_transcript) :]
                                    if result.transcript.startswith(
                                        sent_transcript
                                    )
                                    else ""
                                )
                                sent_transcript = result.transcript
                                if delta:
                                    await router.emit(
                                        node_id,
                                        "speaker_transcripts",
                                        SpeakerTranscriptionResult(
                                            phase="delta",
                                            item_source_id=(
                                                context.item_source_id
                                            ),
                                            segment_id=context.segment_id,
                                            batch_id=context.batch_id,
                                            local_speaker_id=(
                                                context.local_speaker_id
                                            ),
                                            speaker=context.speaker,
                                            source_start_sample=(
                                                context.source_start_sample
                                            ),
                                            source_end_sample=(
                                                context.source_end_sample
                                            ),
                                            start_sample=(
                                                candidate.start_sample
                                            ),
                                            end_sample=candidate.end_sample,
                                            sample_rate=result.sample_rate,
                                            text=delta,
                                            language_code=(
                                                result.language_code
                                            ),
                                        ),
                                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "说话人片段转写失败，继续处理后续片段: %s",
                        exc,
                    )
                    await router.emit(
                        node_id,
                        "speaker_transcripts",
                        SpeakerTranscriptionResult(
                            phase="failed",
                            item_source_id=context.item_source_id,
                            segment_id=context.segment_id,
                            batch_id=context.batch_id,
                            local_speaker_id=context.local_speaker_id,
                            speaker=context.speaker,
                            source_start_sample=context.source_start_sample,
                            source_end_sample=context.source_end_sample,
                            start_sample=candidate.start_sample,
                            end_sample=candidate.end_sample,
                            sample_rate=request.recognition.sample_rate,
                            text="",
                            language_code=state.language_code,
                            error_code="speaker_transcription_failed",
                            error_message=str(exc),
                        ),
                    )
                if final_transcript is not None:
                    state.parts.append(
                        (candidate.start_sample, final_transcript)
                    )
                if context.source_completed:
                    ordered = [
                        text
                        for _, text in sorted(
                            state.parts,
                            key=lambda value: value[0],
                        )
                    ]
                    await router.emit(
                        node_id,
                        "transcripts",
                        AsrInferencer.Result(
                            transcript=_join_transcript_parts(ordered),
                            is_final=True,
                            start_sample=state.start_sample,
                            end_sample=state.end_sample,
                            sample_rate=request.recognition.sample_rate,
                            turn_completed=state.turn_completed,
                            language_code=state.language_code,
                        ),
                    )
                    del source_states[context.item_source_id]
                continue
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

    async def _audio_embedding(self, node_id: str, node: CompiledAudioNode, request: AudioInferenceRequest, router: _EdgeRouter) -> None:
        definition = cast(AudioEmbeddingNode, node.definition)
        inferencer = self._engine_feature_inferencers[node_id]
        async for item in _items(router.input(node_id, "segments")):
            candidate = cast(_SegmentCandidate, item)
            started = time.monotonic()
            try:
                if self._embedding_semaphore is None:
                    vector = await inferencer.embed(candidate.audio, algorithm=definition.config.algorithm, expected_dimension=definition.config.expected_dimension, normalize=definition.config.normalize)
                else:
                    async with self._embedding_semaphore:
                        vector = await inferencer.embed(candidate.audio, algorithm=definition.config.algorithm, expected_dimension=definition.config.expected_dimension, normalize=definition.config.normalize)
                result = AudioEmbeddingResult(
                    candidate.start_sample, candidate.end_sample, request.recognition.sample_rate,
                    vector, latency_ms=(time.monotonic() - started) * 1000,
                )
            except Exception as exc:
                logger.warning("音频向量化失败，ASR 继续: %s", exc)
                result = AudioEmbeddingResult(
                    candidate.start_sample, candidate.end_sample, request.recognition.sample_rate,
                    None, latency_ms=(time.monotonic() - started) * 1000,
                    error_code="embedding_failed", error_message=str(exc),
                )
            await router.emit(node_id, "embeddings", result)

    async def _speaker_diarization(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        definition = cast(SpeakerDiarizationNode, node.definition)
        session = DiarizationSession(
            definition.config,
            request.recognition.sample_rate,
            self._diarization_inferencers[node_id],
        )

        async def candidates():
            async for item in _items(router.input(node_id, "segments")):
                yield cast(_SegmentCandidate, item)

        async for port, payload in session.run(candidates()):
            await router.emit(node_id, port, payload)

    async def _speaker_clustering(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        definition = cast(SpeakerClusteringNode, node.definition)
        session = SpeakerClusteringSession(
            definition.config,
            request.recognition.sample_rate,
            self._engine_feature_inferencers[node_id],
            failure_policy=definition.failure_policy,
            embedding_limiter=self._embedding_semaphore,
        )

        async def candidates():
            async for item in _items(router.input(node_id, "segments")):
                yield cast(_SegmentCandidate, item)

        async for port, payload in session.run(candidates()):
            await router.emit(node_id, port, payload)

    async def _vector_search(self, node_id: str, node: CompiledAudioNode, router: _EdgeRouter) -> None:
        definition = cast(VectorSearchNode, node.definition)
        async for item in _items(router.input(node_id, "embeddings")):
            embedded = cast(AudioEmbeddingResult, item)
            started = time.monotonic()
            groups: tuple[dict[str, Any], ...] = ()
            code = embedded.error_code
            message = embedded.error_message
            if code is None:
                try:
                    if self._vectors is None: raise RuntimeError("Qdrant 尚未配置")
                    found = await self._vectors.search_embedding(
                        definition.config.collection_name,
                        cast(tuple[float, ...], embedded.embedding),
                        vector_name=definition.config.vector_name,
                        top_k=definition.config.top_k,
                        score_threshold=definition.config.score_threshold,
                        filter_json=definition.config.filter,
                    )
                    groups = tuple({
                        "speaker_id": value.speaker_id,
                        "score": value.score,
                        "observation": {
                            "point_id": value.observation.point_id,
                            "payload": value.observation.payload,
                        },
                    } for value in found)
                except Exception as exc:
                    logger.warning("人物搜索失败，ASR 继续: %s", exc)
                    code, message = "vector_search_failed", str(exc)
            await router.emit(node_id, "results", VectorSearchResult(
                start_sample=embedded.start_sample, end_sample=embedded.end_sample, sample_rate=embedded.sample_rate,
                collection_name=definition.config.collection_name,
                vector_name=definition.config.vector_name, groups=groups,
                latency_ms=embedded.latency_ms + (time.monotonic() - started) * 1000,
                embedding=embedded.embedding if definition.config.include_embedding and code is None else None,
                error_code=code, error_message=message,
            ))

    async def _speaker_filter(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        definition = cast(SpeakerFilterNode, node.definition)
        incoming: asyncio.Queue[tuple[str, object | None]] = asyncio.Queue()

        async def pump(port: str) -> None:
            try:
                async for value in _items(router.input(node_id, port)):
                    await incoming.put((port, value))
            finally:
                await incoming.put((port, None))

        pumps = [
            asyncio.create_task(pump("segments"), name=f"{node_id}-segments"),
            asyncio.create_task(pump("results"), name=f"{node_id}-results"),
        ]
        segments: dict[tuple[int, int, int], _SegmentCandidate] = {}
        results: dict[tuple[int, int, int], VectorSearchResult] = {}
        order: list[tuple[int, int, int]] = []
        completed: set[str] = set()

        async def emit_decision(
            candidate: _SegmentCandidate,
            result: VectorSearchResult | None,
        ) -> None:
            speaker_id: str | None = None
            score: float | None = None
            status: Literal["matched", "rejected", "failed"] = "rejected"
            reason: Literal[
                "matched",
                "no_match",
                "speaker_not_allowed",
                "score_too_low",
                "vector_search_failed",
                "input_mismatch",
            ] = "no_match"
            if result is None or result.error_code is not None:
                status = "failed"
                reason = (
                    "input_mismatch"
                    if result is None
                    else "vector_search_failed"
                )
            else:
                allowed = set(definition.config.allowed_speaker_ids)
                allowed_groups = [
                    group
                    for group in result.groups
                    if str(group.get("speaker_id", "")) in allowed
                ]
                eligible = [
                    group
                    for group in allowed_groups
                    if float(group.get("score", float("-inf")))
                    >= definition.config.min_score
                ]
                if eligible:
                    matched = max(
                        eligible,
                        key=lambda group: float(group["score"]),
                    )
                    speaker_id = str(matched["speaker_id"])
                    score = float(matched["score"])
                    status = "matched"
                    reason = "matched"
                elif allowed_groups:
                    best = max(
                        allowed_groups,
                        key=lambda group: float(group.get("score", float("-inf"))),
                    )
                    speaker_id = str(best["speaker_id"])
                    score = float(best.get("score", float("-inf")))
                    reason = "score_too_low"
                elif result.groups:
                    reason = "speaker_not_allowed"
            decision = SpeakerFilterResult(
                start_sample=candidate.start_sample,
                end_sample=candidate.end_sample,
                sample_rate=request.recognition.sample_rate,
                status=status,
                reason=reason,
                speaker_id=speaker_id,
                score=score,
                physical_segment_ids=candidate_physical_segment_ids(
                    candidate
                ),
            )
            await router.emit(node_id, "decisions", decision)
            if status != "matched":
                return
            if request.include_vad_events:
                await router.emit(
                    node_id,
                    "speech_started",
                    SpeechStarted(candidate.start_sample, request.recognition.sample_rate),
                )
                await router.emit(
                    node_id,
                    "speech_stopped",
                    SpeechStopped(
                        candidate.start_sample,
                        candidate.end_sample,
                        request.recognition.sample_rate,
                        physical_segment_ids=candidate_physical_segment_ids(
                            candidate
                        ),
                    ),
                )
            await router.emit(node_id, "segments", candidate)

        try:
            while len(completed) < 2:
                port, value = await incoming.get()
                if value is None:
                    completed.add(port)
                elif port == "segments":
                    candidate = cast(_SegmentCandidate, value)
                    key = (
                        candidate.start_sample,
                        candidate.end_sample,
                        request.recognition.sample_rate,
                    )
                    if key in segments:
                        raise ValueError(f"说话人过滤收到重复音频区间: {key}")
                    segments[key] = candidate
                    order.append(key)
                else:
                    result = cast(VectorSearchResult, value)
                    key = (result.start_sample, result.end_sample, result.sample_rate)
                    if key in results:
                        raise ValueError(f"说话人过滤收到重复检索区间: {key}")
                    results[key] = result
                while order and order[0] in results:
                    ready = order.pop(0)
                    await emit_decision(segments.pop(ready), results.pop(ready))

            for key in order:
                await emit_decision(segments[key], None)
            for result in results.values():
                await router.emit(
                    node_id,
                    "decisions",
                    SpeakerFilterResult(
                        start_sample=result.start_sample,
                        end_sample=result.end_sample,
                        sample_rate=result.sample_rate,
                        status="failed",
                        reason="input_mismatch",
                    ),
                )
        finally:
            for task in pumps:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)

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
