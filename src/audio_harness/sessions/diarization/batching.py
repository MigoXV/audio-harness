from __future__ import annotations

from dataclasses import dataclass, replace
from typing import cast
from uuid import uuid4

from audio_harness.inferencers.diarization.base import DiarizationInferencer
from audio_harness.inferencers.diarization.postprocessor import DiarizationPostprocessor
from audio_harness.sessions.contracts import (
    DiarizationContext,
    SegmentCandidate,
    TimelineSpan,
    candidate_timeline_spans,
    slice_timeline_spans,
)
from audio_harness.workflows.events import DiarizationSegment
from audio_harness.workflows.models import SpeakerDiarizationNodeConfig


@dataclass(frozen=True)
class _DiarizationSourceSpan:
    input_start_sample: int
    input_end_sample: int
    session_start_sample: int
    source_id: str
    source_start_sample: int
    source_end_sample: int
    source_completed: bool
    physical_segment_ids: tuple[str, ...]


@dataclass(frozen=True)
class _DiarizedPiece:
    input_start_sample: int
    input_end_sample: int
    span_index: int
    segment: DiarizationSegment


@dataclass(frozen=True)
class DiarizationBatch:
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
                    start_sample=(
                        span.session_start_sample
                        + overlap_start
                        - span.input_start_sample
                    ),
                    end_sample=(
                        span.session_start_sample
                        + overlap_end
                        - span.input_start_sample
                    ),
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

    def map_processed(
        self,
        value: DiarizationPostprocessor.Segment,
        batch_id: str,
        *,
        min_asr_segment_seconds: float,
        batch_completed: bool = False,
    ) -> tuple[DiarizationSegment | None, SegmentCandidate]:
        if value.end_sample <= value.start_sample:
            raise ValueError("后处理结果必须覆盖非空音频区间")
        overlaps = tuple(
            (
                span,
                max(value.start_sample, span.input_start_sample),
                min(value.end_sample, span.input_end_sample),
            )
            for span in self.spans
            if min(value.end_sample, span.input_end_sample)
            > max(value.start_sample, span.input_start_sample)
        )
        if not overlaps:
            raise ValueError("后处理结果必须落在一个逻辑语义轮次内")
        source_ids = {span.source_id for span, _, _ in overlaps}
        if len(source_ids) != 1:
            raise ValueError("后处理结果不能跨越逻辑语义轮次")
        if (
            overlaps[0][1] != value.start_sample
            or overlaps[-1][2] != value.end_sample
            or any(
                previous_end != current_start
                for (_, _, previous_end), (_, current_start, _) in zip(
                    overlaps,
                    overlaps[1:],
                )
            )
        ):
            raise ValueError("后处理结果没有被 VAD 区间完整覆盖")
        first_span, first_overlap_start, _ = overlaps[0]
        last_span, _, last_overlap_end = overlaps[-1]
        segment_id = f"dseg_{uuid4().hex}"
        session_start = (
            first_span.session_start_sample
            + first_overlap_start
            - first_span.input_start_sample
        )
        session_end = (
            last_span.session_start_sample
            + last_overlap_end
            - last_span.input_start_sample
        )
        local_speaker_id = (
            f"{batch_id}:{value.speaker}"
            if value.speaker is not None
            else None
        )
        mapped = (
            DiarizationSegment(
                segment_id=segment_id,
                start_sample=session_start,
                end_sample=session_end,
                speaker=value.speaker,
                local_speaker_id=cast(str, local_speaker_id),
            )
            if value.speaker is not None
            else None
        )
        source_completed = (
            last_span.source_completed
            and value.end_sample == last_span.input_end_sample
        )
        candidate = SegmentCandidate(
            audio=self.audio[value.start_sample * 2 : value.end_sample * 2],
            start_sample=session_start,
            end_sample=session_end,
            turn_completed=source_completed,
            evaluate_turn=False,
            diarization=DiarizationContext(
                item_source_id=first_span.source_id,
                source_start_sample=first_span.source_start_sample,
                source_end_sample=first_span.source_end_sample,
                source_completed=source_completed,
                segment_id=segment_id,
                batch_id=batch_id,
                speaker=value.speaker,
                local_speaker_id=local_speaker_id,
                min_asr_segment_seconds=(
                    min_asr_segment_seconds if value.speaker is not None else 0
                ),
                batch_completed=batch_completed,
            ),
            timeline_spans=tuple(
                TimelineSpan(
                    input_start_sample=overlap_start - value.start_sample,
                    input_end_sample=overlap_end - value.start_sample,
                    session_start_sample=(
                        span.session_start_sample
                        + overlap_start
                        - span.input_start_sample
                    ),
                )
                for span, overlap_start, overlap_end in overlaps
            ),
            physical_segment_ids=tuple(
                dict.fromkeys(
                    physical_segment_id
                    for span, _, _ in overlaps
                    for physical_segment_id in span.physical_segment_ids
                )
            ),
        )
        return mapped, candidate

    def output_candidates(
        self,
        pieces: tuple[_DiarizedPiece, ...],
        batch_id: str,
        *,
        fallback: bool = False,
        min_asr_segment_seconds: float = 0,
    ) -> tuple[SegmentCandidate, ...]:
        result: list[SegmentCandidate] = []
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
                key=lambda item: (
                    item[0],
                    item[1],
                    item[2].segment.speaker if item[2] is not None else -1,
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
                    SegmentCandidate(
                        audio=self.audio[start * 2 : end * 2],
                        start_sample=(
                            span.session_start_sample
                            + start
                            - span.input_start_sample
                        ),
                        end_sample=(
                            span.session_start_sample
                            + end
                            - span.input_start_sample
                        ),
                        turn_completed=source_completed,
                        evaluate_turn=False,
                        diarization=DiarizationContext(
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
                        physical_segment_ids=span.physical_segment_ids,
                    )
                )
        if result:
            last = result[-1]
            assert last.diarization is not None
            result[-1] = last._replace(
                diarization=replace(last.diarization, batch_completed=True)
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

    def _append_candidate_slice(
        self,
        candidate: SegmentCandidate,
        start_sample: int,
        end_sample: int,
        *,
        source_id: str,
        source_completed: bool,
    ) -> None:
        audio = candidate.audio[start_sample * 2 : end_sample * 2]
        samples = len(audio) // 2
        if samples <= 0:
            return
        input_start = self.pending_samples
        self._audio.extend(audio)
        spans = slice_timeline_spans(
            candidate_timeline_spans(candidate),
            start_sample,
            end_sample,
        )
        for index, span in enumerate(spans):
            self._spans.append(
                _DiarizationSourceSpan(
                    input_start_sample=input_start + span.input_start_sample,
                    input_end_sample=input_start + span.input_end_sample,
                    session_start_sample=span.session_start_sample,
                    source_id=source_id,
                    source_start_sample=candidate.start_sample,
                    source_end_sample=candidate.end_sample,
                    source_completed=(
                        source_completed and index == len(spans) - 1
                    ),
                    physical_segment_ids=candidate.physical_segment_ids,
                )
            )

    def _flush(self) -> DiarizationBatch | None:
        if not self._audio:
            return None
        batch = DiarizationBatch(audio=bytes(self._audio), spans=tuple(self._spans))
        self._audio.clear()
        self._spans.clear()
        return batch

    def accept(self, candidate: SegmentCandidate) -> tuple[DiarizationBatch, ...]:
        if not candidate.audio:
            return ()
        emitted: list[DiarizationBatch] = []
        offset = 0
        total = len(candidate.audio) // 2
        source_id = f"source_{uuid4().hex}"
        if self.pending_samples and self.pending_samples + total > self._max_samples:
            previous = self._flush()
            if previous is not None:
                emitted.append(previous)
        while total - offset > self._max_samples:
            end = offset + self._max_samples
            self._append_candidate_slice(
                candidate,
                offset,
                end,
                source_id=source_id,
                source_completed=False,
            )
            batch = self._flush()
            assert batch is not None
            emitted.append(batch)
            offset = end
        if offset < total:
            self._append_candidate_slice(
                candidate,
                offset,
                total,
                source_id=source_id,
                source_completed=True,
            )
        if self.pending_samples >= self._min_samples:
            batch = self._flush()
            if batch is not None:
                emitted.append(batch)
        return tuple(emitted)

    def finish(self) -> tuple[DiarizationBatch, ...]:
        batch = self._flush()
        return (batch,) if batch is not None else ()
