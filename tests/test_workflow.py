from __future__ import annotations

from collections.abc import AsyncIterator

import asyncio
import pytest

from audio_harness.config import WorkflowConfig
from audio_harness.domain import (
    AudioSegment,
    RecognitionOptions,
    TranscriptionResult,
    VadBoundary,
    VadUpdate,
)
from audio_harness.pipelines.asr_vad import AsrVadWorkflow


class FakeVad:
    def __init__(
        self,
        updates: list[VadUpdate],
        *,
        turn_results: list[bool] | None = None,
        turn_error: Exception | None = None,
    ) -> None:
        self.updates = updates
        self.turn_results = list(turn_results or [])
        self.turn_error = turn_error
        self.received = bytearray()
        self.turn_calls: list[bytes] = []

    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadUpdate]:
        async for chunk in audio_chunks:
            self.received.extend(chunk)
        for update in self.updates:
            yield update

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        self.turn_calls.append(audio)
        if self.turn_error is not None:
            raise self.turn_error
        return self.turn_results.pop(0)


class FakeAsr:
    def __init__(self) -> None:
        self.calls: list[AudioSegment] = []

    async def transcribe(
        self,
        segment: AudioSegment,
        options: RecognitionOptions,
    ) -> AsyncIterator[TranscriptionResult]:
        self.calls.append(segment)
        for transcript, is_final in (("临时", False), ("最终", True)):
            yield TranscriptionResult(
                transcript=transcript,
                is_final=is_final,
                start_sample=segment.start_sample,
                end_sample=segment.end_sample,
                sample_rate=options.sample_rate,
                turn_completed=segment.turn_completed,
                language_code=options.language_code,
            )


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def make_workflow(
    vad: FakeVad,
    asr: FakeAsr,
    *,
    semantic_vad: FakeVad | None = None,
    config: WorkflowConfig | None = None,
) -> AsrVadWorkflow:
    return AsrVadWorkflow(
        asr=asr,
        vad=vad,
        semantic_vad=semantic_vad,
        config=config
        or WorkflowConfig(
            min_chunk_bytes=4,
            min_segment_seconds=0,
            max_segment_seconds=60,
        ),
        asr_semaphore=asyncio.Semaphore(2),
    )


@pytest.mark.asyncio
async def test_semantic_vad_merges_incomplete_turn_and_streams_asr_results() -> None:
    sample_rate = 4
    physical = FakeVad(
        [
            VadUpdate(
                boundaries=(
                    VadBoundary(0, sample_rate),
                    VadBoundary(sample_rate, sample_rate * 2),
                ),
                processed_samples=sample_rate * 2,
            )
        ]
    )
    semantic = FakeVad([], turn_results=[False, True])
    asr = FakeAsr()
    workflow = make_workflow(physical, asr, semantic_vad=semantic)
    options = RecognitionOptions(sample_rate=sample_rate, language_code="zh-CN")

    results = [
        item
        async for item in workflow.run(
            chunks(bytes(sample_rate * 2 * 2)),
            options,
        )
    ]

    assert [(item.transcript, item.is_final, item.turn_completed) for item in results] == [
        ("", True, False),
        ("临时", False, True),
        ("最终", True, True),
    ]
    assert (results[1].start_sample, results[1].end_sample) == (0, sample_rate * 2)
    assert len(asr.calls) == 1
    assert len(asr.calls[0].audio) == sample_rate * 2 * 2


@pytest.mark.asyncio
async def test_open_speech_is_hard_cut_at_max_segment_length() -> None:
    sample_rate = 4
    vad = FakeVad(
        [
            VadUpdate(
                boundaries=(VadBoundary(0, None),),
                processed_samples=sample_rate * 2,
            )
        ]
    )
    asr = FakeAsr()
    workflow = make_workflow(
        vad,
        asr,
        config=WorkflowConfig(
            min_chunk_bytes=4,
            min_segment_seconds=0,
            max_segment_seconds=1,
        ),
    )

    results = [
        item
        async for item in workflow.run(
            chunks(bytes(sample_rate * 2 * 2)),
            RecognitionOptions(sample_rate=sample_rate, language_code="zh-CN"),
        )
    ]

    finals = [item for item in results if item.is_final]
    assert [(item.start_sample, item.end_sample) for item in finals] == [
        (0, sample_rate),
        (sample_rate, sample_rate * 2),
    ]
    assert len(asr.calls) == 2


@pytest.mark.asyncio
async def test_semantic_vad_failure_fails_open() -> None:
    sample_rate = 4
    physical = FakeVad(
        [
            VadUpdate(
                boundaries=(VadBoundary(0, sample_rate),),
                processed_samples=sample_rate,
            )
        ]
    )
    semantic = FakeVad([], turn_error=RuntimeError("unavailable"))
    asr = FakeAsr()
    workflow = make_workflow(physical, asr, semantic_vad=semantic)

    results = [
        item
        async for item in workflow.run(
            chunks(bytes(sample_rate * 2)),
            RecognitionOptions(sample_rate=sample_rate, language_code="zh-CN"),
        )
    ]

    assert [item.transcript for item in results] == ["临时", "最终"]
    assert len(asr.calls) == 1
