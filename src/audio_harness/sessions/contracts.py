from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


class TimelineSpan(NamedTuple):
    """压缩音频区间到会话绝对采样位置的映射。"""

    input_start_sample: int
    input_end_sample: int
    session_start_sample: int

    @property
    def session_end_sample(self) -> int:
        return self.session_start_sample + (
            self.input_end_sample - self.input_start_sample
        )


@dataclass(frozen=True)
class DiarizationContext:
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


class SegmentCandidate(NamedTuple):
    audio: bytes
    start_sample: int
    end_sample: int
    turn_completed: bool = True
    evaluate_turn: bool = True
    diarization: DiarizationContext | None = None
    timeline_spans: tuple[TimelineSpan, ...] = ()
    physical_segment_id: str | None = None
    physical_segment_completed: bool = False


def candidate_timeline_spans(
    candidate: SegmentCandidate,
) -> tuple[TimelineSpan, ...]:
    if candidate.timeline_spans:
        return candidate.timeline_spans
    samples = len(candidate.audio) // 2
    if samples <= 0:
        return ()
    return (TimelineSpan(0, samples, candidate.start_sample),)


def slice_timeline_spans(
    spans: tuple[TimelineSpan, ...],
    start_sample: int,
    end_sample: int,
) -> tuple[TimelineSpan, ...]:
    result: list[TimelineSpan] = []
    for span in spans:
        overlap_start = max(start_sample, span.input_start_sample)
        overlap_end = min(end_sample, span.input_end_sample)
        if overlap_end <= overlap_start:
            continue
        result.append(
            TimelineSpan(
                input_start_sample=overlap_start - start_sample,
                input_end_sample=overlap_end - start_sample,
                session_start_sample=(
                    span.session_start_sample
                    + overlap_start
                    - span.input_start_sample
                ),
            )
        )
    return tuple(result)
