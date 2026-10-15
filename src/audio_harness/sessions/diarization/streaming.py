from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, replace
from uuid import uuid4

from audio_harness.inferencers.diarization.base import DiarizationInferencer
from audio_harness.inferencers.diarization.postprocessor import DiarizationPostprocessor
from audio_harness.inferencers.diarization.streaming_base import StreamingDiarizationInferencer
from audio_harness.media.pcm import PcmChunk
from audio_harness.sessions.contracts import (
    DiarizationContext, SegmentCandidate, SpeechAudioChunk, TimelineSpan, candidate_timeline_spans,
    slice_timeline_spans,
)
from audio_harness.sessions.diarization.batching import DiarizationBatcher
from audio_harness.workflows.events import (
    DiarizationClusterAssignment, DiarizationClusterResult, DiarizationLocalResult, DiarizationSegment,
)
from audio_harness.workflows.models import (
    SpeakerDiarizationNodeConfig, StreamingSpeakerDiarizationConfig,
)

logger = logging.getLogger(__name__)

StreamingEmission = tuple[
    str, DiarizationLocalResult | DiarizationClusterResult | SegmentCandidate,
]


@dataclass
class _Segment:
    speaker: int
    start: int
    end: int | None = None


@dataclass
class _SpeechBuffer:
    source_id: str
    start: int
    audio: bytearray
    started: float
    completed: bool = False
    batch_id: str = ""
    sequence: int = 0
    preview_segments: tuple[DiarizationSegment, ...] = ()

    @property
    def end(self) -> int:
        return self.start + len(self.audio) // 2


class StreamingDiarizationTimeline:
    """按 ID 合并边界；水位前的判定不可修改，闭合顺序不要求按起点排序。"""

    def __init__(self, *, history_samples: int, max_segments: int) -> None:
        self.processed_samples = 0
        self.retained_start = 0
        self._history_samples = history_samples
        self._max_segments = max_segments
        self._segments: dict[int, _Segment] = {}
        self._seen: set[int] = set()

    def accept(self, update: StreamingDiarizationInferencer.Update, sent_samples: int) -> None:
        watermark = update.processed_samples
        if not self.processed_samples <= watermark <= sent_samples:
            raise ValueError("流式分离确定进度倒退或超过已发送音频")
        for value in update.boundaries:
            if value.segment_id < 0 or not 1 <= value.speaker <= 8:
                raise ValueError("流式分离片段 ID 或 speaker 非法")
            current = self._segments.get(value.segment_id)
            if current is None:
                if value.segment_id in self._seen or value.start_sample is None:
                    raise ValueError("流式分离重复片段 ID 或缺少开始边界")
                if not self.processed_samples <= value.start_sample < watermark:
                    raise ValueError("流式分离开始边界超出新确定区间")
                if len(self._seen) >= self._max_segments:
                    raise ValueError("流式分离片段数量超过 max_segments")
                current = _Segment(value.speaker, value.start_sample)
                self._segments[value.segment_id] = current
                self._seen.add(value.segment_id)
            elif (
                current.end is not None or current.speaker != value.speaker
                or (value.start_sample is not None and value.start_sample != current.start)
            ):
                raise ValueError("流式分离修改已闭合片段或已有身份/起点")
            if value.end_sample is not None:
                if not max(current.start + 1, self.processed_samples) <= value.end_sample <= watermark:
                    raise ValueError("流式分离结束边界非法或修改已确定区间")
                current.end = value.end_sample
        self.processed_samples = watermark
        self.retained_start = max(0, watermark - self._history_samples)
        self._segments = {
            key: value for key, value in self._segments.items()
            if value.end is None or value.end > self.retained_start
        }

    def evidence(self, candidate: SegmentCandidate) -> tuple[DiarizationInferencer.Segment, ...]:
        if candidate.start_sample < self.retained_start:
            raise ValueError("轮次超过流式分离时间轴历史保留范围")
        values: list[tuple[int, int, DiarizationInferencer.Segment]] = []
        for key, segment in self._segments.items():
            end = segment.end if segment.end is not None else self.processed_samples
            for span in candidate_timeline_spans(candidate):
                start = max(segment.start, span.session_start_sample)
                stop = min(end, span.session_end_sample)
                if stop > start:
                    offset = span.input_start_sample - span.session_start_sample
                    values.append((start + offset, key, DiarizationInferencer.Segment(
                        start + offset, stop + offset, segment.speaker,
                    )))
        return tuple(value for _, _, value in sorted(values, key=lambda item: (item[0], item[1])))

    def finish(self, sent_samples: int) -> None:
        if self.processed_samples != sent_samples:
            raise ValueError("流式分离最终确定进度没有覆盖全部输入")
        if any(value.end is None for value in self._segments.values()):
            raise ValueError("流式分离正常结束时仍有未闭合片段")


class StreamingDiarizationSession:
    """连续 PCM 推理与 VAD 轮次对齐，产生旧的局部分离及全局身份快照。"""

    def __init__(
        self, config: StreamingSpeakerDiarizationConfig, sample_rate: int,
        inferencer: StreamingDiarizationInferencer, *, queue_capacity: int,
    ) -> None:
        self._config = config
        self._sample_rate = sample_rate
        self._inferencer = inferencer
        self._queue_capacity = queue_capacity
        self._max_samples = max(1, round(config.max_pending_audio_seconds * sample_rate))
        self._timeline = StreamingDiarizationTimeline(
            history_samples=self._max_samples, max_segments=config.max_segments,
        )
        self._sent = 0
        self._audio_eof = False
        self._failure: Exception | None = None
        self._assignments: list[DiarizationClusterAssignment] = []
        self._revision = 0
        self._completed_revision: int | None = None
        self._speech_buffers: deque[_SpeechBuffer] = deque()

    def _process(
        self, value: SegmentCandidate | SpeechAudioChunk, started: float,
        *, batch_id: str | None = None, sequence: int = 0,
    ) -> tuple[StreamingEmission, ...]:
        chunk = value if isinstance(value, SpeechAudioChunk) else None
        candidate = chunk.as_candidate() if chunk is not None else value
        assert isinstance(candidate, SegmentCandidate)
        if chunk is not None and not chunk.audio:
            if not chunk.completed:
                raise ValueError("空语音块必须携带结束标记")
            return (("segments", candidate._replace(diarization=DiarizationContext(
                item_source_id=chunk.physical_segment_id,
                source_start_sample=chunk.source_start_sample,
                source_end_sample=chunk.end_sample,
                source_completed=True,
                segment_id=f"dseg_{uuid4().hex}", batch_id=f"batch_{uuid4().hex}",
                speaker=None, local_speaker_id=None, min_asr_segment_seconds=0,
                batch_completed=True,
            ))),)
        batcher = DiarizationBatcher(SpeakerDiarizationNodeConfig(
            min_batch_seconds=0,
            max_batch_seconds=max(1, len(candidate.audio) / 2 / self._sample_rate),
        ), self._sample_rate)
        batches = batcher.accept(candidate)
        if not batches:
            return ()
        assert len(batches) == 1
        batch = batches[0]
        if chunk is not None:
            batch = replace(batch, spans=tuple(replace(
                span, source_id=chunk.physical_segment_id,
                source_start_sample=chunk.source_start_sample,
                source_completed=chunk.completed and index == len(batch.spans) - 1,
            ) for index, span in enumerate(batch.spans)))
        if chunk is None and batch_id is not None:
            # 增量入口以物理 VAD 段内的说话人轮次为逻辑来源。
            batch = replace(batch, spans=tuple(replace(
                span, source_id=batch_id,
                source_start_sample=candidate.start_sample,
                source_end_sample=candidate.end_sample,
                source_completed=index == len(batch.spans) - 1,
            ) for index, span in enumerate(batch.spans)))
        stable_ids = batch_id is not None
        batch_id = batch_id or f"batch_{uuid4().hex}"
        processor = DiarizationPostprocessor(
            DiarizationPostprocessor.Span(span.input_start_sample, span.input_end_sample, span.source_id)
            for span in batch.spans
        )
        if self._failure is None:
            evidence = self._timeline.evidence(candidate)
            pieces = (*processor.accept(evidence), *processor.finish())
        else:
            pieces = processor.abort()
        mapped = [batch.map_processed(
            piece, batch_id,
            min_asr_segment_seconds=self._config.min_asr_segment_seconds,
            batch_completed=index == len(pieces) - 1,
        ) for index, piece in enumerate(pieces)]
        if stable_ids:
            stable = []
            for segment, output in mapped:
                if segment is not None:
                    segment_id = f"dseg_{batch_id}_{output.start_sample}"
                    segment = replace(segment, segment_id=segment_id)
                    assert output.diarization is not None
                    output = output._replace(diarization=replace(
                        output.diarization, segment_id=segment_id,
                    ))
                stable.append((segment, output))
            mapped = stable
        segments = tuple(segment for segment, _ in mapped if segment is not None)
        if len(self._assignments) + len(segments) > self._config.max_segments:
            raise ValueError("流式分离输出片段数量超过 max_segments")
        latency = (time.monotonic() - started) * 1000
        common = dict(
            batch_id=batch_id, batch_start_sample=batch.start_sample,
            batch_end_sample=batch.end_sample, speech_samples=batch.samples,
            sample_rate=self._sample_rate, segments=segments, latency_ms=latency,
        )
        emissions: list[StreamingEmission] = []
        if self._failure is None:
            if segments:
                emissions.append(("local_results", DiarizationLocalResult(
                    phase="delta", sequence=sequence, **common,
                )))
            emissions.append(("local_results", DiarizationLocalResult(
                phase="completed", sequence=None, **common,
            )))
        else:
            emissions.append(("local_results", DiarizationLocalResult(
                phase="failed", sequence=None, error_code="diarization_failed",
                error_message=str(self._failure), **common,
            )))
        emissions.extend(("segments", value) for _, value in mapped)
        if segments:
            self._assignments.extend(DiarizationClusterAssignment(
                segment.segment_id, segment.local_speaker_id, segment.speaker - 1,
            ) for segment in segments)
            self._revision += 1
            emissions.append(("cluster_results", DiarizationClusterResult(
                phase="completed", revision=self._revision,
                replaces_revision=self._completed_revision,
                assignments=tuple(self._assignments),
            )))
            self._completed_revision = self._revision
        return tuple(emissions)

    def _buffer_speech(self, chunk: SpeechAudioChunk, started: float) -> None:
        if self._speech_buffers and self._speech_buffers[-1].source_id == chunk.physical_segment_id:
            buffer = self._speech_buffers[-1]
            if buffer.completed or buffer.end != chunk.start_sample:
                raise ValueError("VAD 语音块不连续或物理段已经完成")
        else:
            if self._speech_buffers and not self._speech_buffers[-1].completed:
                raise ValueError("新物理段之前缺少语音块结束标记")
            buffer = _SpeechBuffer(
                chunk.physical_segment_id, chunk.start_sample, bytearray(), started,
                batch_id=f"batch_{uuid4().hex}",
            )
            self._speech_buffers.append(buffer)
        buffer.audio.extend(chunk.audio)
        buffer.completed = chunk.completed

    def _speech_candidate(self) -> SegmentCandidate:
        # 每次只处理最早一个物理 VAD 段；后续同身份段也不能跨接。
        buffer = self._speech_buffers[0]
        return SegmentCandidate(
            b"", buffer.start, buffer.end, evaluate_turn=False,
            timeline_spans=(TimelineSpan(0, len(buffer.audio) // 2, buffer.start),),
            physical_segment_ids=(buffer.source_id,),
        )

    def _consume_speech(self, samples: int) -> None:
        while self._speech_buffers and samples:
            buffer = self._speech_buffers[0]
            take = min(samples, len(buffer.audio) // 2)
            del buffer.audio[:take * 2]
            buffer.start += take
            samples -= take
            if not buffer.audio and buffer.completed:
                self._speech_buffers.popleft()
            elif not samples:
                break
        if self._speech_buffers:
            first = self._speech_buffers[0]
            first.batch_id = f"batch_{uuid4().hex}"
            first.sequence = 0
            first.preview_segments = ()

    def _flush_speech(self, *, final: bool = False) -> tuple[StreamingEmission, ...]:
        emissions: list[StreamingEmission] = []
        while self._speech_buffers:
            # 空完成标记关闭物理 VAD 段，不产生额外的空转写。
            while self._speech_buffers and not self._speech_buffers[0].audio:
                if not (self._speech_buffers[0].completed or final):
                    return tuple(emissions)
                self._speech_buffers.popleft()
            if not self._speech_buffers:
                break
            buffer = self._speech_buffers[0]
            candidate = self._speech_candidate()
            samples = candidate.timeline_spans[-1].input_end_sample
            processor = DiarizationPostprocessor((DiarizationPostprocessor.Span(0, samples),))
            if self._failure is None:
                evidence = self._timeline.evidence(candidate)
                ready = processor.accept(evidence)
                # 模型的同 speaker 边界不结束轮次；确认换人或物理 VAD
                # 完成才提交，不能把被 VAD 否决的间隙跨接到下一段。
                pieces = (*ready, *processor.finish()) if final or buffer.completed else ready
            else:
                pieces = processor.abort() if (
                    final or buffer.completed or samples >= self._max_samples
                ) else ()
            if not pieces:
                if self._failure is None:
                    preview = (*ready, *processor.finish())
                    spans = candidate.timeline_spans
                    segments = []
                    for piece in preview:
                        if piece.speaker is None:
                            continue
                        mapped = slice_timeline_spans(spans, piece.start_sample, piece.end_sample)
                        start, end = mapped[0].session_start_sample, mapped[-1].session_end_sample
                        segments.append(DiarizationSegment(
                            segment_id=f"dseg_{buffer.batch_id}_{start}",
                            start_sample=start, end_sample=end, speaker=piece.speaker,
                            local_speaker_id=f"{buffer.batch_id}:{piece.speaker}",
                        ))
                    if segments and tuple(segments) != buffer.preview_segments:
                        emissions.append(("local_results", DiarizationLocalResult(
                            phase="delta", batch_id=buffer.batch_id, sequence=buffer.sequence,
                            batch_start_sample=candidate.start_sample,
                            batch_end_sample=candidate.end_sample, speech_samples=samples,
                            sample_rate=self._sample_rate, segments=tuple(segments),
                            latency_ms=(time.monotonic() - buffer.started) * 1000,
                        )))
                        buffer.sequence += 1
                        buffer.preview_segments = tuple(segments)
                break
            end = pieces[0].end_sample
            spans = slice_timeline_spans(candidate.timeline_spans, 0, end)
            # 只复制即将提交的前缀，而不反复复制持续增长的整段音频。
            remaining = end * 2
            audio = []
            physical_ids = []
            for value in self._speech_buffers:
                take = min(remaining, len(value.audio))
                audio.append(bytes(value.audio[:take]))
                if take:
                    physical_ids.append(value.source_id)
                remaining -= take
                if not remaining:
                    break
            output = candidate._replace(
                audio=b"".join(audio), end_sample=spans[-1].session_end_sample,
                timeline_spans=spans, physical_segment_ids=tuple(dict.fromkeys(physical_ids)),
            )
            emissions.extend(self._process(
                output, buffer.started, batch_id=buffer.batch_id, sequence=buffer.sequence,
            ))
            self._consume_speech(end)
        return tuple(emissions)

    async def run(
        self, audio: AsyncIterator[PcmChunk], candidates: AsyncIterator[SegmentCandidate | SpeechAudioChunk],
    ) -> AsyncIterator[StreamingEmission]:
        events: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=self._queue_capacity)
        audio_queue: asyncio.Queue[PcmChunk | None] = asyncio.Queue(maxsize=self._queue_capacity)
        pending: deque[tuple[SegmentCandidate | SpeechAudioChunk, float]] = deque()
        pending_samples = 0
        candidates_done = updates_done = False
        audio_error: Exception | None = None

        async def collect_audio():
            # Own the graph input independently of the RPC writer. Cancelling
            # a writer awaiting anext() must not close the graph input iterator:
            # fail-open still needs to consume it to release upstream queues.
            nonlocal audio_error
            try:
                async for chunk in audio:
                    await audio_queue.put(chunk)
            except Exception as exc:
                audio_error = exc
            await audio_queue.put(None)
            if audio_error is not None:
                await events.put(("source_failure", audio_error))

        async def raw():
            while (chunk := await audio_queue.get()) is not None:
                if chunk.sample_rate != self._sample_rate or chunk.start_sample != self._sent:
                    raise ValueError("流式分离 PCM 时间轴或采样率不一致")
                self._sent = chunk.end_sample
                yield chunk.pcm_s16le
            self._audio_eof = True

        async def receive():
            try:
                async with aclosing(raw()) as chunks, aclosing(self._inferencer.stream_diarize(
                    chunks, self._sample_rate,
                    input_buffer_latency_ms=self._config.input_buffer_latency_ms,
                )) as updates:
                    async for update in updates:
                        await events.put(("update", update))
                await events.put(("updates_done", None))
            except Exception as exc:
                await events.put(("failure", exc))

        async def collect():
            try:
                async for candidate in candidates:
                    await events.put(("candidate", candidate))
                await events.put(("candidates_done", None))
            except Exception as exc:
                await events.put(("source_failure", exc))

        async def drain_audio():
            while await audio_queue.get() is not None:
                pass

        receiver = asyncio.create_task(receive(), name="diarization-duplex-receive")
        collector = asyncio.create_task(collect(), name="diarization-duplex-turns")
        audio_collector = asyncio.create_task(collect_audio(), name="diarization-duplex-audio")
        drainer: asyncio.Task | None = None

        async def fail(exc: Exception) -> StreamingEmission:
            nonlocal drainer, updates_done
            logger.warning("流式说话人分离失败，剩余轮次直通 ASR: %s", exc)
            self._failure = exc
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            if not self._audio_eof:
                drainer = asyncio.create_task(drain_audio(), name="diarization-duplex-drain")
            updates_done = True
            self._revision += 1
            return "cluster_results", DiarizationClusterResult(
                phase="failed", revision=self._revision,
                replaces_revision=self._completed_revision, assignments=(),
                error_code="diarization_cluster_failed", error_message=str(exc),
            )

        try:
            while not (candidates_done and updates_done and not pending and not self._speech_buffers):
                kind, value = await events.get()
                if kind == "source_failure":
                    assert isinstance(value, Exception)
                    raise value
                try:
                    if kind == "candidate":
                        assert isinstance(value, (SegmentCandidate, SpeechAudioChunk))
                        pending.append((value, time.monotonic()))
                        pending_samples += len(value.audio) // 2
                        if self._failure is None and (
                            pending_samples + sum(len(b.audio) // 2 for b in self._speech_buffers) > self._max_samples
                            or len(pending) > self._config.max_segments
                        ):
                            raise ValueError("流式分离待处理轮次超过缓冲上限")
                    elif kind == "candidates_done":
                        candidates_done = True
                    elif self._failure is None:
                        if kind == "update":
                            assert isinstance(value, StreamingDiarizationInferencer.Update)
                            self._timeline.accept(value, self._sent)
                        elif kind == "updates_done":
                            if not self._audio_eof:
                                raise ValueError("流式分离在完整消费音频前结束")
                            self._timeline.finish(self._sent)
                            updates_done = True
                        elif kind == "failure":
                            assert isinstance(value, Exception)
                            raise value
                except Exception as exc:
                    if self._failure is not None:
                        raise
                    yield await fail(exc)
                while pending:
                    candidate, started = pending[0]
                    if self._failure is None and candidate.end_sample > self._timeline.processed_samples:
                        if updates_done:
                            yield await fail(ValueError("轮次超出完整输入音频范围"))
                        else:
                            break
                    try:
                        if isinstance(candidate, SpeechAudioChunk):
                            self._buffer_speech(candidate, started)
                            emissions = ()
                        else:
                            emissions = self._process(candidate, started)
                    except Exception as exc:
                        if self._failure is not None:
                            raise
                        yield await fail(exc)
                        emissions = self._process(candidate, started)
                    pending.popleft()
                    pending_samples -= len(candidate.audio) // 2
                    for emission in emissions:
                        yield emission
                try:
                    emissions = self._flush_speech(final=candidates_done and updates_done)
                except Exception as exc:
                    if self._failure is not None:
                        raise
                    yield await fail(exc)
                    emissions = self._flush_speech(final=candidates_done and updates_done)
                for emission in emissions:
                    yield emission
            if drainer is not None:
                await drainer
            if audio_error is not None:
                raise audio_error
        finally:
            tasks = [receiver, collector, audio_collector, *([drainer] if drainer is not None else [])]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
