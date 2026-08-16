from __future__ import annotations

import wave
from collections.abc import AsyncIterator, Iterable
from io import BytesIO

from audio_harness.inferencers.vad.base import VadInferencer


SAMPLE_RATE = 16_000
SAMPLE_WIDTH_BYTES = 2
STREAM_CHUNK_SAMPLES = SAMPLE_RATE // 10


async def pcm_chunks(pcm_s16le: bytes) -> AsyncIterator[bytes]:
    chunk_bytes = STREAM_CHUNK_SAMPLES * SAMPLE_WIDTH_BYTES
    for offset in range(0, len(pcm_s16le), chunk_bytes):
        yield pcm_s16le[offset : offset + chunk_bytes]


class VadBoundaryCollector:
    """Collects streaming VAD boundaries into ordered, non-overlapping intervals."""

    def __init__(self, total_samples: int) -> None:
        self._total_samples = max(0, total_samples)
        self._active_start: int | None = None
        self._intervals: list[tuple[int, int]] = []

    def accept(self, update: VadInferencer.Update) -> None:
        for start_sample, end_sample in update.boundaries:
            if start_sample is not None:
                start = min(max(start_sample, 0), self._total_samples)
                self._active_start = (
                    start
                    if self._active_start is None
                    else min(self._active_start, start)
                )
            if end_sample is not None and self._active_start is not None:
                end = min(max(end_sample, 0), self._total_samples)
                if end > self._active_start:
                    self._intervals.append((self._active_start, end))
                self._active_start = None

    def finish(self) -> tuple[tuple[int, int], ...]:
        if self._active_start is not None and self._total_samples > self._active_start:
            self._intervals.append((self._active_start, self._total_samples))
        self._active_start = None
        return merge_intervals(self._intervals)


def merge_intervals(
    intervals: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def concatenate_intervals(
    pcm_s16le: bytes,
    intervals: Iterable[tuple[int, int]],
) -> bytes:
    return b"".join(
        pcm_s16le[start * SAMPLE_WIDTH_BYTES : end * SAMPLE_WIDTH_BYTES]
        for start, end in intervals
    )


def encode_wav(pcm_s16le: bytes) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(SAMPLE_WIDTH_BYTES)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(pcm_s16le)
    return output.getvalue()
