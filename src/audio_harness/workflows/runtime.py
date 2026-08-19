from __future__ import annotations

import asyncio
import logging
import unicodedata
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple, cast
from uuid import uuid4

from audio_harness.errors import CapacityExceededError
from audio_harness.inferencers import Inferencer
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.inferencers.embedding import (
    AudioEmbeddingInferencer,
    StreamingClusteringSession,
)
from audio_harness.inferencers.diarization import DiarizationInferencer
from audio_harness.media.pcm import PcmChunk
from audio_harness.workflows.compiler import (
    CompiledAudioGraph,
    CompiledAudioNode,
    PortKind,
)
from audio_harness.workflows.events import (
    AudioEmbeddingResult,
    DiarizationClusterAssignment,
    DiarizationClusterResult,
    DiarizationLocalResult,
    DiarizationSegment,
    SpeakerTranscriptionResult,
    SpeechStarted,
    SpeechStopped,
    VadProgress,
    VectorSearchResult,
)
from audio_harness.workflows.models import (
    AsrNode,
    FailurePolicy,
    GraphInputRef,
    NodeOutputRef,
    PhysicalVadConfig,
    PhysicalVadNode,
    SpeakerDiarizationNode,
    SpeakerDiarizationNodeConfig,
    SpeakerClusteringNode,
    SemanticVadConfig,
    SemanticVadNode,
    AudioEmbeddingNode,
    VectorSearchNode,
)
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
    | DiarizationLocalResult
    | SpeakerTranscriptionResult
    | DiarizationClusterResult
)


@dataclass(frozen=True)
class AudioInferenceRequest:
    recognition: AsrInferencer.Options
    include_vad_events: bool = False
    include_vad_progress: bool = False
    include_interim_transcripts: bool = False
    include_vector_search: bool = False
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


class _SegmentCandidate(NamedTuple):
    audio: bytes
    start_sample: int
    end_sample: int
    turn_completed: bool = True
    evaluate_turn: bool = True
    diarization: "_DiarizationContext | None" = None


@dataclass(frozen=True)
class _DiarizationContext:
    item_source_id: str
    source_start_sample: int
    source_end_sample: int
    source_completed: bool
    segment_id: str
    batch_id: str
    speaker: int | None
    local_speaker_id: str | None
    min_asr_segment_seconds: float
    batch_completed: bool = False


@dataclass(frozen=True)
class _DiarizationSourceSpan:
    input_start_sample: int
    input_end_sample: int
    session_start_sample: int
    source_id: str
    source_start_sample: int
    source_end_sample: int
    source_completed: bool


@dataclass(frozen=True)
class _DiarizedPiece:
    input_start_sample: int
    input_end_sample: int
    span_index: int
    segment: DiarizationSegment


@dataclass
class _SourceTranscriptState:
    start_sample: int
    end_sample: int
    turn_completed: bool
    parts: list[tuple[int, str]] = field(default_factory=list)
    language_code: str | None = None


@dataclass(frozen=True)
class _ClusterVector:
    local_speaker_id: str
    segment_ids: tuple[str, ...]
    vector: bytes


@dataclass(frozen=True)
class _DiarizationBatch:
    audio: bytes
    spans: tuple[_DiarizationSourceSpan, ...]

    @property
    def samples(self) -> int:
        return len(self.audio) // 2

    @property
    def start_sample(self) -> int:
        return self.spans[0].session_start_sample

    @property
    def end_sample(self) -> int:
        span = self.spans[-1]
        return span.session_start_sample + (
            span.input_end_sample - span.input_start_sample
        )

    def map_response(
        self,
        values: tuple[DiarizationInferencer.Segment, ...],
        batch_id: str,
    ) -> tuple[tuple[DiarizationSegment, ...], tuple[_DiarizedPiece, ...]]:
        mapped: list[DiarizationSegment] = []
        pieces: list[_DiarizedPiece] = []
        for value in values:
            start = max(0, min(value.start_sample, self.samples))
            end = max(0, min(value.end_sample, self.samples))
            if end <= start:
                continue
            for span_index, span in enumerate(self.spans):
                overlap_start = max(start, span.input_start_sample)
                overlap_end = min(end, span.input_end_sample)
                if overlap_end <= overlap_start:
                    continue
                segment_id = f"dseg_{uuid4().hex}"
                segment = DiarizationSegment(
                    segment_id=segment_id,
                    start_sample=span.session_start_sample
                    + overlap_start
                    - span.input_start_sample,
                    end_sample=span.session_start_sample
                    + overlap_end
                    - span.input_start_sample,
                    speaker=value.speaker,
                    local_speaker_id=f"{batch_id}:{value.speaker}",
                )
                mapped.append(segment)
                pieces.append(
                    _DiarizedPiece(
                        input_start_sample=overlap_start,
                        input_end_sample=overlap_end,
                        span_index=span_index,
                        segment=segment,
                    )
                )
        return tuple(mapped), tuple(pieces)

    def map_segments(
        self,
        values: tuple[DiarizationInferencer.Segment, ...],
    ) -> tuple[DiarizationSegment, ...]:
        """保留给批处理与映射单测使用；正式执行使用 map_response。"""
        mapped, _ = self.map_response(values, "batch_mapping")
        return mapped

    def output_candidates(
        self,
        pieces: tuple[_DiarizedPiece, ...],
        batch_id: str,
        *,
        fallback: bool = False,
        min_asr_segment_seconds: float = 0,
    ) -> tuple[_SegmentCandidate, ...]:
        result: list[_SegmentCandidate] = []
        for span_index, span in enumerate(self.spans):
            assigned = [] if fallback else sorted(
                (piece for piece in pieces if piece.span_index == span_index),
                key=lambda piece: (
                    piece.input_start_sample,
                    piece.input_end_sample,
                    piece.segment.speaker,
                ),
            )
            ranges: list[tuple[int, int, _DiarizedPiece | None]] = [
                (piece.input_start_sample, piece.input_end_sample, piece)
                for piece in assigned
            ]
            cursor = span.input_start_sample
            for start, end, _ in sorted(ranges):
                if start > cursor:
                    ranges.append((cursor, start, None))
                cursor = max(cursor, end)
            if cursor < span.input_end_sample:
                ranges.append((cursor, span.input_end_sample, None))
            if not ranges:
                ranges.append((span.input_start_sample, span.input_end_sample, None))
            ordered = sorted(
                ranges,
                key=lambda value: (
                    value[0],
                    value[1],
                    value[2].segment.speaker if value[2] is not None else -1,
                ),
            )
            for index, (start, end, piece) in enumerate(ordered):
                if end <= start:
                    continue
                segment_id = (
                    piece.segment.segment_id
                    if piece is not None
                    else f"dseg_{uuid4().hex}"
                )
                speaker = piece.segment.speaker if piece is not None else None
                local_speaker_id = (
                    piece.segment.local_speaker_id if piece is not None else None
                )
                source_completed = (
                    span.source_completed and index == len(ordered) - 1
                )
                result.append(
                    _SegmentCandidate(
                        audio=self.audio[start * 2 : end * 2],
                        start_sample=span.session_start_sample
                        + start
                        - span.input_start_sample,
                        end_sample=span.session_start_sample
                        + end
                        - span.input_start_sample,
                        turn_completed=source_completed,
                        evaluate_turn=False,
                        diarization=_DiarizationContext(
                            item_source_id=span.source_id,
                            source_start_sample=span.source_start_sample,
                            source_end_sample=span.source_end_sample,
                            source_completed=source_completed,
                            segment_id=segment_id,
                            batch_id=batch_id,
                            speaker=speaker,
                            local_speaker_id=local_speaker_id,
                            min_asr_segment_seconds=(
                                0 if fallback else min_asr_segment_seconds
                            ),
                        ),
                    )
                )
        if result:
            last = result[-1]
            assert last.diarization is not None
            result[-1] = last._replace(
                diarization=replace(
                    last.diarization,
                    batch_completed=True,
                )
            )
        return tuple(result)


class DiarizationBatcher:
    """按有效语音时长聚合 VAD 块，并保留会话时间轴映射。"""

    def __init__(
        self,
        config: SpeakerDiarizationNodeConfig,
        sample_rate: int,
    ) -> None:
        self._min_samples = int(round(config.min_batch_seconds * sample_rate))
        self._max_samples = max(
            1,
            int(round(config.max_batch_seconds * sample_rate)),
        )
        self._audio = bytearray()
        self._spans: list[_DiarizationSourceSpan] = []

    @property
    def pending_samples(self) -> int:
        return len(self._audio) // 2

    def _append(
        self,
        audio: bytes,
        session_start_sample: int,
        *,
        source_id: str,
        source_start_sample: int,
        source_end_sample: int,
        source_completed: bool,
    ) -> None:
        samples = len(audio) // 2
        if samples <= 0:
            return
        input_start = self.pending_samples
        self._audio.extend(audio)
        self._spans.append(
            _DiarizationSourceSpan(
                input_start_sample=input_start,
                input_end_sample=input_start + samples,
                session_start_sample=session_start_sample,
                source_id=source_id,
                source_start_sample=source_start_sample,
                source_end_sample=source_end_sample,
                source_completed=source_completed,
            )
        )

    def _flush(self) -> _DiarizationBatch | None:
        if not self._audio:
            return None
        batch = _DiarizationBatch(
            audio=bytes(self._audio),
            spans=tuple(self._spans),
        )
        self._audio.clear()
        self._spans.clear()
        return batch

    def accept(self, candidate: _SegmentCandidate) -> tuple[_DiarizationBatch, ...]:
        if not candidate.audio:
            return ()
        emitted: list[_DiarizationBatch] = []
        offset = 0
        total = len(candidate.audio) // 2
        source_id = f"source_{uuid4().hex}"
        if self.pending_samples and self.pending_samples + total > self._max_samples:
            previous = self._flush()
            if previous is not None:
                emitted.append(previous)
        while total - offset > self._max_samples:
            end = offset + self._max_samples
            self._append(
                candidate.audio[offset * 2 : end * 2],
                candidate.start_sample + offset,
                source_id=source_id,
                source_start_sample=candidate.start_sample,
                source_end_sample=candidate.end_sample,
                source_completed=False,
            )
            batch = self._flush()
            assert batch is not None
            emitted.append(batch)
            offset = end
        if offset < total:
            self._append(
                candidate.audio[offset * 2 :],
                candidate.start_sample + offset,
                source_id=source_id,
                source_start_sample=candidate.start_sample,
                source_end_sample=candidate.end_sample,
                source_completed=True,
            )
        if self.pending_samples >= self._min_samples:
            batch = self._flush()
            if batch is not None:
                emitted.append(batch)
        return tuple(emitted)

    def finish(self) -> tuple[_DiarizationBatch, ...]:
        batch = self._flush()
        return (batch,) if batch is not None else ()


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


PhysicalEmission = tuple[
    str,
    _SegmentCandidate | SpeechStarted | SpeechStopped,
]


class VadProgressLimiter:
    """将下游 VAD 游标归一化为受墙钟频率限制的进度事件。"""

    def __init__(
        self,
        *,
        interval_seconds: float,
        sample_rate: int,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("VAD 进度间隔必须大于 0")
        if sample_rate <= 0:
            raise ValueError("VAD 进度采样率必须大于 0")
        self._interval_seconds = interval_seconds
        self._sample_rate = sample_rate
        self._latest_samples = 0
        self._last_emitted_at: float | None = None

    def update(
        self,
        processed_samples: int,
        *,
        available_samples: int,
        now: float | None = None,
    ) -> VadProgress | None:
        normalized = max(
            self._latest_samples,
            min(max(processed_samples, 0), max(available_samples, 0)),
        )
        self._latest_samples = normalized
        observed_at = time.monotonic() if now is None else now
        if (
            self._last_emitted_at is not None
            and observed_at - self._last_emitted_at < self._interval_seconds
        ):
            return None
        self._last_emitted_at = observed_at
        return VadProgress(
            processed_samples=normalized,
            sample_rate=self._sample_rate,
        )

    def finish(self, processed_samples: int) -> VadProgress:
        self._latest_samples = max(processed_samples, 0)
        return VadProgress(
            processed_samples=self._latest_samples,
            sample_rate=self._sample_rate,
            final=True,
        )


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

    @property
    def end_sample(self) -> int:
        return self._audio.end_sample

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
                    PortKind.AUDIO_SEGMENT,
                    PortKind.DIARIZATION_SEGMENT,
                }
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
        for source, names in (implicit_bindings or {}).items():
            self._bindings.setdefault(source, []).extend(names)

    def has_consumers(self, node_id: str, port: str) -> bool:
        source = (node_id, port)
        return bool(self._outgoing.get(source) or self._bindings.get(source))

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
                (
                    SpeechStarted,
                    SpeechStopped,
                    VadProgress,
                    AsrInferencer.Result,
                    VectorSearchResult,
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
        self._embedding_inferencers: dict[str, AudioEmbeddingInferencer] = {}
        self._diarization_inferencers: dict[str, DiarizationInferencer] = {}
        for node_id, node in plan.nodes.items():
            if node.engine_snapshot is None:
                continue
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
            elif node.spec.kind == "audio_embedding":
                if not isinstance(inferencer, AudioEmbeddingInferencer):
                    raise TypeError(f"节点 {node_id} 需要 AudioEmbeddingInferencer")
                self._embedding_inferencers[node_id] = inferencer
            elif node.spec.kind == "speaker_clustering":
                if not isinstance(inferencer, AudioEmbeddingInferencer):
                    raise TypeError(
                        f"节点 {node_id} 需要 AudioEmbeddingInferencer"
                    )
                self._embedding_inferencers[node_id] = inferencer
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
                "diarization_local",
                "speaker_transcript",
                "diarization_cluster",
            }
        )
        if request.include_interim_transcripts:
            active_outputs.add("interim_transcript")
        if request.include_vector_search:
            active_outputs.add("vector_search")
        if request.include_diarization:
            active_outputs.add("diarization_local")
            active_outputs.add("speaker_transcript")
            active_outputs.add("diarization_cluster")
        active_nodes = _output_ancestors(self._plan, active_outputs)
        implicit_bindings: dict[tuple[str, str], tuple[str, ...]] = {}
        if request.include_vad_progress:
            speech_started = next(
                (
                    binding
                    for binding in self._plan.definition.outputs
                    if binding.name == "speech_started"
                ),
                None,
            )
            if speech_started is not None:
                source_node = self._plan.nodes.get(
                    speech_started.source.node_id
                )
                if (
                    source_node is not None
                    and isinstance(source_node.definition, PhysicalVadNode)
                    and source_node.definition.config.progress_interval_seconds > 0
                ):
                    implicit_bindings[
                        (speech_started.source.node_id, "progress")
                    ] = ("vad_progress",)
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
        progress = (
            VadProgressLimiter(
                interval_seconds=definition.config.progress_interval_seconds,
                sample_rate=request.recognition.sample_rate,
            )
            if router.has_consumers(node_id, "progress")
            else None
        )
        input_drained = False

        async def recorded() -> AsyncIterator[bytes]:
            nonlocal input_drained
            async for item in _items(queue):
                chunk = cast(PcmChunk, item)
                state.append(chunk)
                yield chunk.pcm_s16le
            input_drained = True

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
            if progress is not None:
                event = progress.update(
                    update.processed_samples,
                    available_samples=state.end_sample,
                )
                if event is not None:
                    await router.emit(node_id, "progress", event)
        await publish(state.finish())
        if progress is not None and input_drained:
            await router.emit(
                node_id,
                "progress",
                progress.finish(state.end_sample),
            )

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
                segment_samples = candidate.end_sample - candidate.start_sample
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
        inferencer = self._embedding_inferencers[node_id]
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
        inferencer = self._diarization_inferencers[node_id]
        batcher = DiarizationBatcher(
            definition.config,
            request.recognition.sample_rate,
        )

        async def process(batch: _DiarizationBatch) -> None:
            batch_id = f"batch_{uuid4().hex}"
            started = time.monotonic()
            all_segments: list[DiarizationSegment] = []
            all_pieces: list[_DiarizedPiece] = []
            sequence = 0
            try:
                async for values in inferencer.stream_diarize(
                    batch.audio,
                    request.recognition.sample_rate,
                ):
                    mapped, pieces = batch.map_response(values, batch_id)
                    all_segments.extend(mapped)
                    all_pieces.extend(pieces)
                    await router.emit(
                        node_id,
                        "local_results",
                        DiarizationLocalResult(
                            phase="delta",
                            batch_id=batch_id,
                            sequence=sequence,
                            batch_start_sample=batch.start_sample,
                            batch_end_sample=batch.end_sample,
                            speech_samples=batch.samples,
                            sample_rate=request.recognition.sample_rate,
                            segments=mapped,
                            latency_ms=(time.monotonic() - started) * 1000,
                        ),
                    )
                    sequence += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("说话人分离失败，主工作流继续: %s", exc)
                await router.emit(
                    node_id,
                    "local_results",
                    DiarizationLocalResult(
                        phase="failed",
                        batch_id=batch_id,
                        sequence=None,
                        batch_start_sample=batch.start_sample,
                        batch_end_sample=batch.end_sample,
                        speech_samples=batch.samples,
                        sample_rate=request.recognition.sample_rate,
                        segments=tuple(all_segments),
                        latency_ms=(time.monotonic() - started) * 1000,
                        error_code="diarization_failed",
                        error_message=str(exc),
                    ),
                )
                for candidate in batch.output_candidates(
                    (),
                    batch_id,
                    fallback=True,
                    min_asr_segment_seconds=(
                        definition.config.min_asr_segment_seconds
                    ),
                ):
                    await router.emit(node_id, "segments", candidate)
                return
            await router.emit(
                node_id,
                "local_results",
                DiarizationLocalResult(
                    phase="completed",
                    batch_id=batch_id,
                    sequence=None,
                    batch_start_sample=batch.start_sample,
                    batch_end_sample=batch.end_sample,
                    speech_samples=batch.samples,
                    sample_rate=request.recognition.sample_rate,
                    segments=tuple(all_segments),
                    latency_ms=(time.monotonic() - started) * 1000,
                ),
            )
            for candidate in batch.output_candidates(
                tuple(all_pieces),
                batch_id,
                min_asr_segment_seconds=(
                    definition.config.min_asr_segment_seconds
                ),
            ):
                await router.emit(node_id, "segments", candidate)

        async for item in _items(router.input(node_id, "segments")):
            candidate = cast(_SegmentCandidate, item)
            for batch in batcher.accept(candidate):
                await process(batch)
        for batch in batcher.finish():
            await process(batch)

    async def _speaker_clustering(
        self,
        node_id: str,
        node: CompiledAudioNode,
        request: AudioInferenceRequest,
        router: _EdgeRouter,
    ) -> None:
        definition = cast(SpeakerClusteringNode, node.definition)
        config = definition.config
        inferencer = self._embedding_inferencers[node_id]
        sample_rate = request.recognition.sample_rate
        max_embedding_samples = max(
            1,
            int(round(config.max_embedding_seconds * sample_rate)),
        )
        vectors: list[_ClusterVector] = []
        failed_assignments: list[DiarizationClusterAssignment] = []
        pending: list[_SegmentCandidate] = []
        session: StreamingClusteringSession | None = None
        revision = 0
        last_completed_revision: int | None = None
        last_labels: tuple[int, ...] = ()

        def assignments(labels: tuple[int, ...]) -> tuple[DiarizationClusterAssignment, ...]:
            if len(labels) != len(vectors):
                raise ValueError(
                    "流式聚类返回标签数量不符: "
                    f"expected={len(vectors)}, actual={len(labels)}"
                )
            values = list(failed_assignments)
            for record, label in zip(vectors, labels, strict=True):
                values.extend(
                    DiarizationClusterAssignment(
                        segment_id=segment_id,
                        local_speaker_id=record.local_speaker_id,
                        speaker_id=label,
                    )
                    for segment_id in record.segment_ids
                )
            return tuple(values)

        async def embed(audio: bytes) -> bytes:
            kwargs = {
                "algorithm": config.embedding_algorithm,
                "expected_dimension": config.expected_dimension,
                "normalize": config.normalize,
            }
            if self._embedding_semaphore is None:
                return await inferencer.embed_bytes(audio, **kwargs)
            async with self._embedding_semaphore:
                return await inferencer.embed_bytes(audio, **kwargs)

        async def process_batch(candidates: tuple[_SegmentCandidate, ...]) -> None:
            nonlocal session, revision, last_completed_revision, last_labels
            grouped: dict[str, list[_SegmentCandidate]] = {}
            for candidate in candidates:
                context = candidate.diarization
                if context is None or context.local_speaker_id is None:
                    continue
                grouped.setdefault(context.local_speaker_id, []).append(candidate)
            if not grouped:
                return

            revision += 1
            new_vectors: list[_ClusterVector] = []
            for local_speaker_id, pieces in grouped.items():
                pieces.sort(key=lambda value: (value.start_sample, value.end_sample))
                segment_ids = tuple(
                    cast(_DiarizationContext, piece.diarization).segment_id
                    for piece in pieces
                )
                audio = b"".join(piece.audio for piece in pieces)
                audio = audio[: max_embedding_samples * 2]
                try:
                    vector = await embed(audio)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "说话人聚类向量提取失败，片段标记为噪声: %s",
                        exc,
                    )
                    failed_assignments.extend(
                        DiarizationClusterAssignment(
                            segment_id=segment_id,
                            local_speaker_id=local_speaker_id,
                            speaker_id=-1,
                        )
                        for segment_id in segment_ids
                    )
                    continue
                new_vectors.append(
                    _ClusterVector(
                        local_speaker_id=local_speaker_id,
                        segment_ids=segment_ids,
                        vector=vector,
                    )
                )

            if len(vectors) + len(new_vectors) > config.max_vectors:
                error = ValueError(
                    f"说话人聚类向量数量超过上限 {config.max_vectors}"
                )
                await router.emit(
                    node_id,
                    "results",
                    DiarizationClusterResult(
                        phase="failed",
                        revision=revision,
                        replaces_revision=last_completed_revision,
                        assignments=(),
                        error_code="diarization_cluster_failed",
                        error_message=str(error),
                    ),
                )
                if definition.failure_policy is FailurePolicy.FAIL_PIPELINE:
                    raise error
                return

            vectors.extend(new_vectors)
            started = time.monotonic()
            try:
                if len(vectors) < 2:
                    labels = tuple(-1 for _ in vectors)
                elif session is None:
                    session = inferencer.streaming_clustering()
                    labels = await session.start(
                        tuple(item.vector for item in vectors),
                        algorithm=config.clustering_algorithm,
                        parameters=config.parameters.model_dump(mode="json"),
                    )
                elif new_vectors:
                    labels = await session.add(
                        tuple(item.vector for item in new_vectors)
                    )
                else:
                    labels = last_labels
                snapshot = assignments(labels)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("说话人流式聚类失败，保留上一版结果: %s", exc)
                if session is not None:
                    await session.close()
                    session = None
                await router.emit(
                    node_id,
                    "results",
                    DiarizationClusterResult(
                        phase="failed",
                        revision=revision,
                        replaces_revision=last_completed_revision,
                        assignments=(),
                        error_code="diarization_cluster_failed",
                        error_message=str(exc),
                    ),
                )
                if definition.failure_policy is FailurePolicy.FAIL_PIPELINE:
                    raise
                return

            await router.emit(
                node_id,
                "results",
                DiarizationClusterResult(
                    phase="completed",
                    revision=revision,
                    replaces_revision=last_completed_revision,
                    assignments=snapshot,
                ),
            )
            last_completed_revision = revision
            last_labels = labels
            logger.debug(
                "说话人聚类完成: revision=%s vectors=%s latency_ms=%.2f",
                revision,
                len(vectors),
                (time.monotonic() - started) * 1000,
            )

        try:
            async for item in _items(router.input(node_id, "segments")):
                candidate = cast(_SegmentCandidate, item)
                context = candidate.diarization
                if context is None:
                    raise ValueError("说话人聚类节点只能接收说话人分离片段")
                pending.append(candidate)
                if context.batch_completed:
                    batch_id = context.batch_id
                    batch = tuple(
                        value
                        for value in pending
                        if cast(_DiarizationContext, value.diarization).batch_id
                        == batch_id
                    )
                    pending = [
                        value
                        for value in pending
                        if cast(_DiarizationContext, value.diarization).batch_id
                        != batch_id
                    ]
                    await process_batch(batch)
        finally:
            if session is not None:
                await session.close()

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
