from __future__ import annotations

import time
from collections.abc import AsyncIterator
from uuid import uuid4

from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.media.pcm import PcmChunk, VadAudioChunk
from audio_harness.sessions.contracts import SegmentCandidate
from audio_harness.workflows.events import SpeechStarted, SpeechStopped, VadProgress
from audio_harness.workflows.models import PhysicalVadConfig


PhysicalEmission = tuple[
    str,
    SegmentCandidate | SpeechStarted | SpeechStopped | VadProgress,
]


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

    def append(self, pcm_s16le: bytes) -> None:
        if len(pcm_s16le) % 2:
            raise ValueError("PCM S16LE 音频字节数必须为偶数")
        size = len(pcm_s16le)
        if size >= self._capacity_bytes:
            tail = pcm_s16le[-self._capacity_bytes :]
            self._storage[:] = tail
            self._start_byte = 0
            self._length_bytes = self._capacity_bytes
        elif size:
            overflow = max(0, self._length_bytes + size - self._capacity_bytes)
            if overflow:
                self._start_byte = (
                    self._start_byte + overflow
                ) % self._capacity_bytes
                self._length_bytes -= overflow
            write_at = (
                self._start_byte + self._length_bytes
            ) % self._capacity_bytes
            first = min(size, self._capacity_bytes - write_at)
            self._storage[write_at : write_at + first] = pcm_s16le[:first]
            if first < size:
                self._storage[: size - first] = pcm_s16le[first:]
            self._length_bytes += size
        self._end_sample += size // 2

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


class VadProgressLimiter:
    """将下游 VAD 游标归一化为受墙钟频率限制的进度事件。"""

    def __init__(self, *, interval_seconds: float, sample_rate: int) -> None:
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
        return VadProgress(processed_samples=normalized, sample_rate=self._sample_rate)

    def finish(self, processed_samples: int) -> VadProgress:
        self._latest_samples = max(processed_samples, 0)
        return VadProgress(
            processed_samples=self._latest_samples,
            sample_rate=self._sample_rate,
            final=True,
        )


class PhysicalVadSession:
    """单个物理 VAD 节点的有界音频、推理与边界状态。"""

    def __init__(
        self,
        config: PhysicalVadConfig,
        sample_rate: int,
        inferencer: VadInferencer,
    ) -> None:
        self._config = config
        self._sample_rate = sample_rate
        self._inferencer = inferencer
        self._min_samples = int(round(config.min_segment_seconds * sample_rate))
        self._max_samples = max(1, int(round(config.max_segment_seconds * sample_rate)))
        self._pre_roll_samples = int(round(config.speech_pre_roll_seconds * sample_rate))
        capacity = max(1, int(round(config.audio_buffer_seconds * sample_rate)))
        self._audio = RollingPcmBuffer(capacity)
        self._speech_start: int | None = None
        self._segment_start: int | None = None
        self._physical_segment_id: str | None = None
        self._last_processed = 0

    @property
    def end_sample(self) -> int:
        return self._audio.end_sample

    def append(self, chunk: PcmChunk) -> None:
        if chunk.start_sample != self._audio.end_sample:
            raise ValueError("物理 VAD 收到不连续的音频块")
        self._audio.append(chunk.pcm_s16le)

    def _segment(
        self,
        start: int,
        end: int,
        *,
        evaluate_turn: bool,
        physical_segment_completed: bool = False,
    ) -> PhysicalEmission | None:
        start = max(start, self._audio.base_sample)
        end = min(end, self._audio.end_sample)
        if end <= start or end - start < self._min_samples:
            return None
        return (
            "segments",
            SegmentCandidate(
                audio=self._audio.slice(start, end),
                start_sample=start,
                end_sample=end,
                turn_completed=True,
                evaluate_turn=evaluate_turn,
                physical_segment_id=self._physical_segment_id,
                physical_segment_completed=physical_segment_completed,
                physical_segment_ids=(self._physical_segment_id,),
            ),
        )

    def _close_active_segment(
        self,
        end: int,
        *,
        evaluate_turn: bool,
        reopen: bool,
    ) -> list[PhysicalEmission]:
        assert self._speech_start is not None
        assert self._segment_start is not None
        assert self._physical_segment_id is not None
        result: list[PhysicalEmission] = []
        item = self._segment(
            self._segment_start,
            end,
            evaluate_turn=evaluate_turn,
            physical_segment_completed=True,
        )
        if item is not None:
            result.append(item)
        result.append(
            (
                "speech_stopped",
                SpeechStopped(
                    start_sample=self._speech_start,
                    end_sample=end,
                    sample_rate=self._sample_rate,
                    physical_segment_id=self._physical_segment_id,
                    physical_segment_ids=(self._physical_segment_id,),
                ),
            )
        )
        if reopen:
            self._speech_start = end
            self._segment_start = end
            self._physical_segment_id = f"pseg_{uuid4().hex}"
            result.append(
                (
                    "speech_started",
                    SpeechStarted(
                        start_sample=end,
                        sample_rate=self._sample_rate,
                        physical_segment_id=self._physical_segment_id,
                    ),
                )
            )
        else:
            self._speech_start = None
            self._segment_start = None
            self._physical_segment_id = None
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
                if self._speech_start is None:
                    self._speech_start = start
                    self._segment_start = start
                    self._physical_segment_id = f"pseg_{uuid4().hex}"
                    result.append(
                        (
                            "speech_started",
                            SpeechStarted(
                                start_sample=start,
                                sample_rate=self._sample_rate,
                                physical_segment_id=self._physical_segment_id,
                            ),
                        )
                    )
                else:
                    previous_start = self._speech_start
                    self._speech_start = min(self._speech_start, start)
                    if self._segment_start == previous_start:
                        self._segment_start = min(self._segment_start, start)
            if end_sample is not None:
                end = min(max(end_sample, 0), self._audio.end_sample)
                if self._speech_start is not None and end > self._speech_start:
                    assert self._segment_start is not None
                    while self._segment_start + self._max_samples < end:
                        result.extend(
                            self._close_active_segment(
                                self._segment_start + self._max_samples,
                                evaluate_turn=False,
                                reopen=True,
                            )
                        )
                    result.extend(
                        self._close_active_segment(
                            end,
                            evaluate_turn=True,
                            reopen=False,
                        )
                    )
                else:
                    self._speech_start = None
                    self._segment_start = None
                    self._physical_segment_id = None
        if self._segment_start is not None:
            while self._segment_start + self._max_samples < processed:
                result.extend(
                    self._close_active_segment(
                        self._segment_start + self._max_samples,
                        evaluate_turn=False,
                        reopen=True,
                    )
                )
        return result

    def finish(self) -> list[PhysicalEmission]:
        if self._speech_start is None or self._audio.end_sample <= self._speech_start:
            self._speech_start = None
            self._segment_start = None
            self._physical_segment_id = None
            return []
        assert self._segment_start is not None
        end = self._audio.end_sample
        result: list[PhysicalEmission] = []
        while self._segment_start + self._max_samples < end:
            result.extend(
                self._close_active_segment(
                    self._segment_start + self._max_samples,
                    evaluate_turn=False,
                    reopen=True,
                )
            )
        result.extend(
            self._close_active_segment(
                end,
                evaluate_turn=True,
                reopen=False,
            ),
        )
        return result

    async def run(
        self,
        audio: AsyncIterator[PcmChunk | VadAudioChunk],
        *,
        progress_enabled: bool,
    ) -> AsyncIterator[PhysicalEmission]:
        progress = (
            VadProgressLimiter(
                interval_seconds=self._config.progress_interval_seconds,
                sample_rate=self._sample_rate,
            )
            if progress_enabled
            else None
        )
        input_drained = False

        async def recorded() -> AsyncIterator[bytes]:
            nonlocal input_drained
            async for item in audio:
                if isinstance(item, VadAudioChunk):
                    self.append(
                        PcmChunk(
                            item.original_pcm_s16le,
                            item.sample_rate,
                            item.start_sample,
                        )
                    )
                    yield item.vad_pcm_s16le
                else:
                    self.append(item)
                    yield item.pcm_s16le
            input_drained = True

        postprocessing = self._config.postprocessing
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
            self._inferencer.stream_detect(
                recorded(),
                self._sample_rate,
                postprocessing=request_postprocessing,
            )
            if request_postprocessing is not None
            else self._inferencer.stream_detect(recorded(), self._sample_rate)
        )
        async for update in updates:
            if not isinstance(update, VadInferencer.Update):
                raise TypeError(f"非法 VAD 更新类型: {type(update)!r}")
            for emission in self.update(update):
                yield emission
            if progress is not None:
                event = progress.update(
                    update.processed_samples,
                    available_samples=self.end_sample,
                )
                if event is not None:
                    yield "progress", event
        for emission in self.finish():
            yield emission
        if progress is not None and input_drained:
            yield "progress", progress.finish(self.end_sample)
