from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

from audio_harness.inferencers.diarization.base import DiarizationInferencer


class DiarizationPostprocessor:
    """将流式说话人时间戳收敛为完整且不可撤回的语义轮次区间。"""

    class Span(NamedTuple):
        start_sample: int
        end_sample: int
        group_id: str | None = None

    class Segment(NamedTuple):
        start_sample: int
        end_sample: int
        speaker: int | None

    @dataclass
    class _SpanState:
        span: "DiarizationPostprocessor.Span"
        ready: deque["DiarizationPostprocessor.Segment"] = field(
            default_factory=deque
        )
        pending_start: int | None = None
        pending_evidence_end: int | None = None
        pending_speaker: int | None = None
        committed_until: int | None = None
        closed: bool = False

        def __post_init__(self) -> None:
            self.committed_until = self.span.start_sample

    def __init__(self, spans: Iterable[Span]) -> None:
        normalized = tuple(spans)
        if not normalized:
            raise ValueError("diarization 后处理至少需要一个 VAD 区间")
        if normalized[0].start_sample != 0:
            raise ValueError("首个 VAD 区间必须从压缩 batch 的 0 采样开始")
        previous_end = 0
        for span in normalized:
            if span.start_sample != previous_end:
                raise ValueError("VAD 区间必须按压缩 batch 时间轴连续排列")
            if span.end_sample <= span.start_sample:
                raise ValueError("VAD 区间结束位置必须晚于开始位置")
            previous_end = span.end_sample
        grouped: list[DiarizationPostprocessor.Span] = []
        completed_groups: set[str] = set()
        for span in normalized:
            if (
                span.group_id is not None
                and grouped
                and grouped[-1].group_id == span.group_id
            ):
                previous = grouped[-1]
                grouped[-1] = self.Span(
                    previous.start_sample,
                    span.end_sample,
                    span.group_id,
                )
                continue
            if span.group_id is not None:
                if span.group_id in completed_groups:
                    raise ValueError("同一逻辑轮次的 VAD 区间必须连续")
                completed_groups.add(span.group_id)
            grouped.append(span)
        self._states = [self._SpanState(span) for span in grouped]
        self._total_samples = normalized[-1].end_sample
        self._drain_index = 0
        self._last_start_sample: int | None = None
        self._closed = False

    def accept(
        self,
        values: Iterable[DiarizationInferencer.Segment],
    ) -> tuple[Segment, ...]:
        self._ensure_open()
        for value in values:
            if (
                self._last_start_sample is not None
                and value.start_sample < self._last_start_sample
            ):
                raise ValueError("diarization 结果起点跨响应倒退")
            self._last_start_sample = value.start_sample
            if value.speaker < 0:
                raise ValueError("diarization speaker 不能为负数")
            start = max(0, min(value.start_sample, self._total_samples))
            end = max(0, min(value.end_sample, self._total_samples))
            if end <= start:
                continue
            self._close_passed_spans(start)
            for state_index, state in enumerate(self._states):
                if state.closed:
                    continue
                overlap_start = max(start, state.span.start_sample)
                overlap_end = min(end, state.span.end_sample)
                if overlap_end <= overlap_start:
                    continue
                if state.span.group_id is not None:
                    # 一个引擎 segment 可能横跨两个逻辑轮次；只要已经进入
                    # 后一轮次，前一轮次就必须先成为不可撤回结果。
                    for previous in self._states[:state_index]:
                        self._finish_span(previous)
                self._accept_evidence(
                    state,
                    overlap_start,
                    overlap_end,
                    value.speaker,
                )
        return self._drain_ready()

    def finish(self) -> tuple[Segment, ...]:
        self._ensure_open()
        for state in self._states:
            self._finish_span(state)
        self._closed = True
        return self._drain_ready()

    def abort(self) -> tuple[Segment, ...]:
        self._ensure_open()
        result: list[DiarizationPostprocessor.Segment] = []
        for state in self._states:
            state.ready.clear()
            start = state.committed_until
            assert start is not None
            if start < state.span.end_sample:
                result.append(
                    self.Segment(start, state.span.end_sample, None)
                )
            state.pending_start = None
            state.pending_evidence_end = None
            state.pending_speaker = None
            state.committed_until = state.span.end_sample
            state.closed = True
        self._drain_index = len(self._states)
        self._closed = True
        return tuple(result)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("diarization 后处理器已经结束")

    def _close_passed_spans(self, watermark: int) -> None:
        # 最后一个 span 必须由 finish/abort 关闭，确保总有最终候选携带
        # batch_completed，供下游聚类提交本批状态。
        for state in self._states[:-1]:
            if not state.closed and state.span.end_sample <= watermark:
                self._finish_span(state)

    def _accept_evidence(
        self,
        state: _SpanState,
        start: int,
        end: int,
        speaker: int,
    ) -> None:
        if state.pending_start is None:
            state.pending_start = state.span.start_sample
            state.pending_evidence_end = end
            state.pending_speaker = speaker
            return
        assert state.pending_evidence_end is not None
        assert state.pending_speaker is not None
        if state.pending_speaker == speaker:
            state.pending_evidence_end = max(state.pending_evidence_end, end)
            return
        if state.pending_evidence_end <= start:
            boundary = (state.pending_evidence_end + start) // 2
        else:
            # 重叠区归给后到达的新 speaker，使旧区间在此刻即可稳定提交。
            boundary = start
        boundary = max(state.pending_start, min(boundary, state.span.end_sample))
        if boundary > state.pending_start:
            state.ready.append(
                self.Segment(
                    state.pending_start,
                    boundary,
                    state.pending_speaker,
                )
            )
        state.pending_start = boundary
        state.pending_evidence_end = max(boundary, end)
        state.pending_speaker = speaker

    def _finish_span(self, state: _SpanState) -> None:
        if state.closed:
            return
        if state.pending_start is None:
            state.ready.append(
                self.Segment(
                    state.span.start_sample,
                    state.span.end_sample,
                    None,
                )
            )
        elif state.pending_start < state.span.end_sample:
            state.ready.append(
                self.Segment(
                    state.pending_start,
                    state.span.end_sample,
                    state.pending_speaker,
                )
            )
        state.closed = True

    def _drain_ready(self) -> tuple[Segment, ...]:
        result: list[DiarizationPostprocessor.Segment] = []
        while self._drain_index < len(self._states):
            state = self._states[self._drain_index]
            while state.ready:
                segment = state.ready.popleft()
                result.append(segment)
                state.committed_until = segment.end_sample
            if not state.closed:
                break
            self._drain_index += 1
        return tuple(result)
