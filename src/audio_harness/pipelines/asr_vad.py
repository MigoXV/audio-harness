from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from audio_harness.config import WorkflowConfig
from audio_harness.domain import (
    AsyncAsrEngine,
    AsyncVadEngine,
    AudioSegment,
    RecognitionOptions,
    TranscriptionResult,
    VadUpdate,
)

logger = logging.getLogger(__name__)

_END = object()


@dataclass(frozen=True)
class _AsrJob:
    segment: AudioSegment


@dataclass(frozen=True)
class _ImmediateJob:
    result: TranscriptionResult


@dataclass(frozen=True)
class _Failure:
    error: BaseException


class _PcmBuffer:
    def __init__(self) -> None:
        self.base_sample = 0
        self.data = bytearray()

    @property
    def end_sample(self) -> int:
        return self.base_sample + len(self.data) // 2

    def append(self, audio: bytes) -> None:
        if len(audio) % 2:
            raise ValueError("PCM S16LE 音频字节数必须为偶数")
        self.data.extend(audio)

    def slice(self, start_sample: int, end_sample: int) -> bytes:
        if start_sample < self.base_sample or end_sample > self.end_sample:
            raise ValueError(
                "VAD 时间戳超出已记录音频范围: "
                f"[{start_sample}, {end_sample}) not in "
                f"[{self.base_sample}, {self.end_sample})"
            )
        start = (start_sample - self.base_sample) * 2
        end = (end_sample - self.base_sample) * 2
        return bytes(self.data[start:end])

    def trim_before(self, sample: int) -> None:
        target = min(max(sample, self.base_sample), self.end_sample)
        byte_count = (target - self.base_sample) * 2
        if byte_count:
            del self.data[:byte_count]
            self.base_sample = target


class AsrVadWorkflow:
    """协议无关的异步 ASR + VAD 会话工作流。"""

    def __init__(
        self,
        *,
        asr: AsyncAsrEngine,
        vad: AsyncVadEngine,
        semantic_vad: AsyncVadEngine | None,
        config: WorkflowConfig,
        asr_semaphore: asyncio.Semaphore,
    ) -> None:
        self.asr = asr
        self.vad = vad
        self.semantic_vad = semantic_vad
        self.config = config
        self.asr_semaphore = asr_semaphore

    async def run(
        self,
        audio_chunks: AsyncIterator[bytes],
        options: RecognitionOptions,
    ) -> AsyncIterator[TranscriptionResult]:
        jobs: asyncio.Queue[object] = asyncio.Queue(
            maxsize=self.config.segment_queue_capacity
        )
        results: asyncio.Queue[object] = asyncio.Queue(
            maxsize=self.config.result_queue_capacity
        )
        segmenter = asyncio.create_task(
            self._segment_audio(audio_chunks, options, jobs),
            name="audio-harness-segmenter",
        )
        worker = asyncio.create_task(
            self._produce_results(jobs, results, options),
            name="audio-harness-asr-worker",
        )
        supervisor = asyncio.create_task(
            self._supervise((segmenter, worker), results),
            name="audio-harness-workflow-supervisor",
        )

        try:
            while True:
                item = await results.get()
                if item is _END:
                    break
                if isinstance(item, _Failure):
                    raise item.error
                assert isinstance(item, TranscriptionResult)
                yield item
        finally:
            if not supervisor.done():
                supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)

    async def _supervise(
        self,
        tasks: tuple[asyncio.Task[None], asyncio.Task[None]],
        results: asyncio.Queue[object],
    ) -> None:
        failure: BaseException | None = None
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    failure = task.exception()
                    break
            if failure is not None:
                for task in pending:
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            if failure is not None:
                await results.put(_Failure(failure))
            await results.put(_END)

    async def _segment_audio(
        self,
        audio_chunks: AsyncIterator[bytes],
        options: RecognitionOptions,
        jobs: asyncio.Queue[object],
    ) -> None:
        sample_rate = options.sample_rate
        min_samples = int(round(self.config.min_segment_seconds * sample_rate))
        max_samples = max(1, int(round(self.config.max_segment_seconds * sample_rate)))
        audio = _PcmBuffer()
        active_start: int | None = None
        pending_turn_start: int | None = None
        last_processed = 0

        async def recorded_chunks() -> AsyncIterator[bytes]:
            async for chunk in audio_chunks:
                audio.append(chunk)
                yield chunk

        async def enqueue_asr(start: int, end: int) -> None:
            if end <= start:
                return
            await jobs.put(
                _AsrJob(
                    AudioSegment(
                        audio=audio.slice(start, end),
                        start_sample=start,
                        end_sample=end,
                        turn_completed=True,
                    )
                )
            )

        async def enqueue_incomplete(start: int, end: int) -> None:
            await jobs.put(
                _ImmediateJob(
                    TranscriptionResult(
                        transcript="",
                        is_final=True,
                        start_sample=start,
                        end_sample=end,
                        sample_rate=sample_rate,
                        turn_completed=False,
                    )
                )
            )

        async def process_physical(start: int, end: int) -> None:
            nonlocal pending_turn_start
            start = max(start, audio.base_sample)
            end = min(end, audio.end_sample)
            if end - start < min_samples:
                return

            pieces: list[tuple[int, int]] = []
            current = start
            while current + max_samples < end:
                pieces.append((current, current + max_samples))
                current += max_samples
            pieces.append((current, end))

            for piece_start, piece_end in pieces:
                completed = True
                if self.semantic_vad is not None:
                    try:
                        completed = await self.semantic_vad.detect_turn(
                            audio.slice(piece_start, piece_end),
                            sample_rate,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning(
                            "语义 VAD 调用失败，按轮次完成放行: %.3fs-%.3fs",
                            piece_start / sample_rate,
                            piece_end / sample_rate,
                            exc_info=True,
                        )
                        completed = True

                if pending_turn_start is None:
                    pending_turn_start = piece_start

                if completed:
                    while pending_turn_start + max_samples < piece_end:
                        cut = pending_turn_start + max_samples
                        await enqueue_asr(pending_turn_start, cut)
                        pending_turn_start = cut
                    await enqueue_asr(pending_turn_start, piece_end)
                    pending_turn_start = None
                    continue

                forced_any = False
                while pending_turn_start + max_samples <= piece_end:
                    cut = pending_turn_start + max_samples
                    await enqueue_asr(pending_turn_start, cut)
                    pending_turn_start = cut
                    forced_any = True
                if pending_turn_start < piece_end:
                    await enqueue_incomplete(
                        pending_turn_start if forced_any else piece_start,
                        piece_end,
                    )
                else:
                    pending_turn_start = None

        async for update in self.vad.stream_detect(recorded_chunks(), sample_rate):
                if not isinstance(update, VadUpdate):
                    raise TypeError(f"非法 VAD 更新类型: {type(update)!r}")
                last_processed = max(last_processed, update.processed_samples)
                processed = min(last_processed, audio.end_sample)

                for boundary in update.boundaries:
                    if boundary.start_sample is not None:
                        start = min(max(boundary.start_sample, 0), audio.end_sample)
                        if active_start is None:
                            active_start = start
                        else:
                            active_start = min(active_start, start)
                    if boundary.end_sample is not None:
                        end = min(max(boundary.end_sample, 0), audio.end_sample)
                        if active_start is not None and end > active_start:
                            await process_physical(active_start, end)
                        active_start = None

                if active_start is not None:
                    origin = (
                        pending_turn_start
                        if pending_turn_start is not None
                        else active_start
                    )
                    while origin + max_samples <= processed:
                        cut = origin + max_samples
                        await enqueue_asr(origin, cut)
                        origin = cut
                        pending_turn_start = None
                        active_start = max(active_start, cut)

                retain = [
                    value
                    for value in (active_start, pending_turn_start)
                    if value is not None
                ]
                audio.trim_before(min(retain) if retain else processed)

        stream_end = audio.end_sample
        if active_start is not None and stream_end > active_start:
            await process_physical(active_start, stream_end)
        await jobs.put(_END)

    async def _produce_results(
        self,
        jobs: asyncio.Queue[object],
        results: asyncio.Queue[object],
        options: RecognitionOptions,
    ) -> None:
        while True:
            job = await jobs.get()
            if job is _END:
                return
            if isinstance(job, _ImmediateJob):
                await results.put(job.result)
                continue
            assert isinstance(job, _AsrJob)
            async with self.asr_semaphore:
                async for result in self.asr.transcribe(job.segment, options):
                    await results.put(result)
