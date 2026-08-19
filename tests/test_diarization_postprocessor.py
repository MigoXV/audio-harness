from __future__ import annotations

import pytest

from audio_harness.inferencers.diarization import (
    DiarizationInferencer,
    DiarizationPostprocessor,
)


RawSegment = DiarizationInferencer.Segment
ProcessedSegment = DiarizationPostprocessor.Segment
Span = DiarizationPostprocessor.Span


def test_merges_same_speaker_and_fills_span_head_tail_and_gaps() -> None:
    state = DiarizationPostprocessor((Span(0, 100),))

    assert state.accept((RawSegment(10, 30, 1),)) == ()
    assert state.accept((RawSegment(35, 50, 1),)) == ()
    assert state.finish() == (ProcessedSegment(0, 100, 1),)


def test_splits_different_speakers_at_gap_midpoint_and_streams_previous() -> None:
    state = DiarizationPostprocessor((Span(0, 100),))

    assert state.accept((RawSegment(10, 30, 1),)) == ()
    assert state.accept((RawSegment(40, 60, 2),)) == (
        ProcessedSegment(0, 35, 1),
    )
    assert state.finish() == (ProcessedSegment(35, 100, 2),)


def test_splits_overlap_at_new_speaker_start_for_irrevocable_commit() -> None:
    state = DiarizationPostprocessor((Span(0, 100),))

    assert state.accept((RawSegment(10, 50, 1),)) == ()
    assert state.accept((RawSegment(40, 70, 2),)) == (
        ProcessedSegment(0, 40, 1),
    )
    assert state.finish() == (ProcessedSegment(40, 100, 2),)


def test_preserves_vad_span_boundaries_even_for_same_speaker() -> None:
    state = DiarizationPostprocessor((Span(0, 50), Span(50, 100)))

    assert state.accept((RawSegment(10, 90, 1),)) == ()
    assert state.finish() == (
        ProcessedSegment(0, 50, 1),
        ProcessedSegment(50, 100, 1),
    )


def test_emits_empty_vad_span_as_unknown_before_later_evidence() -> None:
    state = DiarizationPostprocessor((Span(0, 50), Span(50, 100)))

    assert state.accept((RawSegment(60, 80, 2),)) == (
        ProcessedSegment(0, 50, None),
    )
    assert state.finish() == (ProcessedSegment(50, 100, 2),)


def test_clamps_ranges_and_drops_empty_values() -> None:
    state = DiarizationPostprocessor((Span(0, 100),))

    assert state.accept(
        (
            RawSegment(-10, 20, 1),
            RawSegment(20, 20, 2),
            RawSegment(80, 120, 2),
        )
    ) == (ProcessedSegment(0, 50, 1),)
    assert state.finish() == (ProcessedSegment(50, 100, 2),)


def test_rejects_cross_response_start_regression() -> None:
    state = DiarizationPostprocessor((Span(0, 100),))
    assert state.accept((RawSegment(20, 40, 1),)) == ()

    with pytest.raises(ValueError, match="起点跨响应倒退"):
        state.accept((RawSegment(19, 50, 2),))


def test_abort_keeps_committed_prefix_and_returns_only_unknown_tail() -> None:
    state = DiarizationPostprocessor((Span(0, 100),))
    assert state.accept((RawSegment(10, 30, 1),)) == ()
    assert state.accept((RawSegment(40, 60, 2),)) == (
        ProcessedSegment(0, 35, 1),
    )

    assert state.abort() == (ProcessedSegment(35, 100, None),)


def test_rejects_invalid_spans_and_calls_after_completion() -> None:
    with pytest.raises(ValueError, match="必须从压缩 batch"):
        DiarizationPostprocessor((Span(1, 10),))
    with pytest.raises(ValueError, match="连续排列"):
        DiarizationPostprocessor((Span(0, 10), Span(11, 20)))

    state = DiarizationPostprocessor((Span(0, 10),))
    assert state.finish() == (ProcessedSegment(0, 10, None),)
    with pytest.raises(RuntimeError, match="已经结束"):
        state.finish()
