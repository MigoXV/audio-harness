from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from uuid import uuid4

from audio_harness.inferencers.diarization import (
    DiarizationInferencer,
    DiarizationPostprocessor,
)
from audio_harness.sessions.contracts import SegmentCandidate
from audio_harness.sessions.diarization.batching import (
    DiarizationBatch,
    DiarizationBatcher,
)
from audio_harness.workflows.events import (
    DiarizationLocalResult,
    DiarizationSegment,
)
from audio_harness.workflows.models import SpeakerDiarizationNodeConfig

logger = logging.getLogger(__name__)

DiarizationEmission = tuple[str, DiarizationLocalResult | SegmentCandidate]


class DiarizationSession:
    """单个说话人分离节点的批处理、推理、映射与失败恢复会话。"""

    def __init__(
        self,
        config: SpeakerDiarizationNodeConfig,
        sample_rate: int,
        inferencer: DiarizationInferencer,
    ) -> None:
        self._config = config
        self._sample_rate = sample_rate
        self._inferencer = inferencer
        self._batcher = DiarizationBatcher(config, sample_rate)

    async def _process_legacy(
        self,
        batch: DiarizationBatch,
    ) -> AsyncIterator[DiarizationEmission]:
        batch_id = f"batch_{uuid4().hex}"
        started = time.monotonic()
        all_segments: list[DiarizationSegment] = []
        all_pieces = []
        sequence = 0
        try:
            async for values in self._inferencer.stream_diarize(
                batch.audio,
                self._sample_rate,
            ):
                mapped, pieces = batch.map_response(values, batch_id)
                all_segments.extend(mapped)
                all_pieces.extend(pieces)
                yield (
                    "local_results",
                    DiarizationLocalResult(
                        phase="delta",
                        batch_id=batch_id,
                        sequence=sequence,
                        batch_start_sample=batch.start_sample,
                        batch_end_sample=batch.end_sample,
                        speech_samples=batch.samples,
                        sample_rate=self._sample_rate,
                        segments=mapped,
                        latency_ms=(time.monotonic() - started) * 1000,
                    ),
                )
                sequence += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("说话人分离失败，主工作流继续: %s", exc)
            yield (
                "local_results",
                DiarizationLocalResult(
                    phase="failed",
                    batch_id=batch_id,
                    sequence=None,
                    batch_start_sample=batch.start_sample,
                    batch_end_sample=batch.end_sample,
                    speech_samples=batch.samples,
                    sample_rate=self._sample_rate,
                    segments=tuple(all_segments),
                    latency_ms=(time.monotonic() - started) * 1000,
                    error_code="diarization_failed",
                    error_message=str(exc),
                ),
            )
            for candidate in batch.output_candidates(
                (),
                batch_id,
                fallback=True,
                min_asr_segment_seconds=self._config.min_asr_segment_seconds,
            ):
                yield "segments", candidate
            return
        yield (
            "local_results",
            DiarizationLocalResult(
                phase="completed",
                batch_id=batch_id,
                sequence=None,
                batch_start_sample=batch.start_sample,
                batch_end_sample=batch.end_sample,
                speech_samples=batch.samples,
                sample_rate=self._sample_rate,
                segments=tuple(all_segments),
                latency_ms=(time.monotonic() - started) * 1000,
            ),
        )
        for candidate in batch.output_candidates(
            tuple(all_pieces),
            batch_id,
            min_asr_segment_seconds=self._config.min_asr_segment_seconds,
        ):
            yield "segments", candidate

    async def _process_postprocessed(
        self,
        batch: DiarizationBatch,
    ) -> AsyncIterator[DiarizationEmission]:
        batch_id = f"batch_{uuid4().hex}"
        started = time.monotonic()
        all_segments: list[DiarizationSegment] = []
        sequence = 0
        postprocessor = DiarizationPostprocessor(
            DiarizationPostprocessor.Span(
                span.input_start_sample,
                span.input_end_sample,
                span.source_id,
            )
            for span in batch.spans
        )
        postprocessor_finished = False

        async def processed(
            values: tuple[DiarizationPostprocessor.Segment, ...],
            *,
            batch_completed: bool = False,
            emit_delta: bool = True,
        ) -> AsyncIterator[DiarizationEmission]:
            nonlocal sequence
            mapped: list[DiarizationSegment] = []
            candidates: list[SegmentCandidate] = []
            for index, value in enumerate(values):
                segment, candidate = batch.map_processed(
                    value,
                    batch_id,
                    min_asr_segment_seconds=self._config.min_asr_segment_seconds,
                    batch_completed=(
                        batch_completed and index == len(values) - 1
                    ),
                )
                if segment is not None:
                    mapped.append(segment)
                candidates.append(candidate)
            if mapped and emit_delta:
                all_segments.extend(mapped)
                yield (
                    "local_results",
                    DiarizationLocalResult(
                        phase="delta",
                        batch_id=batch_id,
                        sequence=sequence,
                        batch_start_sample=batch.start_sample,
                        batch_end_sample=batch.end_sample,
                        speech_samples=batch.samples,
                        sample_rate=self._sample_rate,
                        segments=tuple(mapped),
                        latency_ms=(time.monotonic() - started) * 1000,
                    ),
                )
                sequence += 1
            for candidate in candidates:
                yield "segments", candidate

        try:
            async for values in self._inferencer.stream_diarize(
                batch.audio,
                self._sample_rate,
            ):
                async for emission in processed(postprocessor.accept(values)):
                    yield emission
            tail = postprocessor.finish()
            postprocessor_finished = True
            async for emission in processed(tail, batch_completed=True):
                yield emission
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if postprocessor_finished:
                raise
            logger.warning("说话人分离后处理失败，主工作流继续: %s", exc)
            yield (
                "local_results",
                DiarizationLocalResult(
                    phase="failed",
                    batch_id=batch_id,
                    sequence=None,
                    batch_start_sample=batch.start_sample,
                    batch_end_sample=batch.end_sample,
                    speech_samples=batch.samples,
                    sample_rate=self._sample_rate,
                    segments=tuple(all_segments),
                    latency_ms=(time.monotonic() - started) * 1000,
                    error_code="diarization_failed",
                    error_message=str(exc),
                ),
            )
            async for emission in processed(
                postprocessor.abort(),
                batch_completed=True,
                emit_delta=False,
            ):
                yield emission
            return
        yield (
            "local_results",
            DiarizationLocalResult(
                phase="completed",
                batch_id=batch_id,
                sequence=None,
                batch_start_sample=batch.start_sample,
                batch_end_sample=batch.end_sample,
                speech_samples=batch.samples,
                sample_rate=self._sample_rate,
                segments=tuple(all_segments),
                latency_ms=(time.monotonic() - started) * 1000,
            ),
        )

    async def _process(
        self,
        batch: DiarizationBatch,
    ) -> AsyncIterator[DiarizationEmission]:
        stream = (
            self._process_legacy(batch)
            if self._config.postprocessing is None
            else self._process_postprocessed(batch)
        )
        async for emission in stream:
            yield emission

    async def run(
        self,
        candidates: AsyncIterator[SegmentCandidate],
    ) -> AsyncIterator[DiarizationEmission]:
        async for candidate in candidates:
            for batch in self._batcher.accept(candidate):
                async for emission in self._process(batch):
                    yield emission
        for batch in self._batcher.finish():
            async for emission in self._process(batch):
                yield emission
