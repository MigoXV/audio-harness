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
    SpeechStarted,
    SpeechStopped,
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


class _RollingPcmBuffer:
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

        overflow = max(
            0,
            self._length_bytes + input_bytes - self._capacity_bytes,
        )
        if overflow:
            self._start_byte = (
                self._start_byte + overflow
            ) % self._capacity_bytes
            self._length_bytes -= overflow

        write_at = (
            self._start_byte + self._length_bytes
        ) % self._capacity_bytes
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
        return bytes(self._storage[read_at:]) + bytes(
            self._storage[: size - first]
        )

    def trim_before(self, sample: int) -> None:
        target = min(max(sample, self.base_sample), self.end_sample)
        byte_count = (target - self.base_sample) * 2
        if byte_count:
            self._start_byte = (
                self._start_byte + byte_count
            ) % self._capacity_bytes
            self._length_bytes -= byte_count
            if self._length_bytes == 0:
                self._start_byte = 0


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
        *,
        include_vad_events: bool = False,
        include_vad_progress: bool = False,
    ) -> AsyncIterator[
        TranscriptionResult | SpeechStarted | SpeechStopped | VadUpdate
    ]:
        jobs: asyncio.Queue[object] = asyncio.Queue(
            maxsize=self.config.segment_queue_capacity
        )
        results: asyncio.Queue[object] = asyncio.Queue(
            maxsize=self.config.result_queue_capacity
        )
        segmenter = asyncio.create_task(
            self._segment_audio(
                audio_chunks,
                options,
                jobs,
                results,
                include_vad_events=include_vad_events,
                include_vad_progress=include_vad_progress,
            ),
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
                assert isinstance(
                    item,
                    (
                        TranscriptionResult,
                        SpeechStarted,
                        SpeechStopped,
                        VadUpdate,
                    ),
                )
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
        results: asyncio.Queue[object],
        *,
        include_vad_events: bool,
        include_vad_progress: bool,
    ) -> None:
        sample_rate = options.sample_rate
        min_samples = int(round(self.config.min_segment_seconds * sample_rate))
        max_samples = max(1, int(round(self.config.max_segment_seconds * sample_rate)))
        pre_roll_samples = int(
            round(self.config.speech_pre_roll_seconds * sample_rate)
        )
        buffer_samples = max(
            1,
            int(round(self.config.audio_buffer_seconds * sample_rate)),
        )
        audio = _RollingPcmBuffer(buffer_samples)
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
                if include_vad_progress:
                    await results.put(update)
                last_processed = max(last_processed, update.processed_samples)
                processed = min(last_processed, audio.end_sample)

                for boundary in update.boundaries:
                    if boundary.start_sample is not None:
                        start = min(
                            max(
                                boundary.start_sample - pre_roll_samples,
                                audio.base_sample,
                                0,
                            ),
                            audio.end_sample,
                        )
                        if active_start is None:
                            active_start = start
                            if include_vad_events:
                                await results.put(
                                    SpeechStarted(
                                        start_sample=start,
                                        sample_rate=sample_rate,
                                    )
                                )
                        else:
                            active_start = min(active_start, start)
                    if boundary.end_sample is not None:
                        end = min(max(boundary.end_sample, 0), audio.end_sample)
                        if active_start is not None and end > active_start:
                            if include_vad_events:
                                await results.put(
                                    SpeechStopped(
                                        start_sample=active_start,
                                        end_sample=end,
                                        sample_rate=sample_rate,
                                    )
                                )
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

                # VAD 可能在处理若干后续帧后才回报更早的语音起点。这里不能按
                # processed_time 主动裁剪；固定容量环形缓冲会自行淘汰过期音频。

        stream_end = audio.end_sample
        if active_start is not None and stream_end > active_start:
            if include_vad_events:
                await results.put(
                    SpeechStopped(
                        start_sample=active_start,
                        end_sample=stream_end,
                        sample_rate=sample_rate,
                    )
                )
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
