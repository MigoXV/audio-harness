from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.sessions.contracts import (
    SegmentCandidate,
    TimelineSpan,
    candidate_physical_segment_ids,
    candidate_timeline_spans,
    slice_timeline_spans,
)
from audio_harness.workflows.events import SpeechStopped
from audio_harness.workflows.models import FailurePolicy, SemanticVadConfig

logger = logging.getLogger(__name__)

SemanticEmission = tuple[
    str,
    SegmentCandidate | SpeechStopped | AsrInferencer.Result,
]


class SemanticVadSession:
    """单个语义 VAD 节点的轮次合并与推理状态。"""

    def __init__(
        self,
        config: SemanticVadConfig,
        sample_rate: int,
        inferencer: VadInferencer,
    ) -> None:
        self._sample_rate = sample_rate
        self._inferencer = inferencer
        self._max_samples = max(1, int(round(config.max_buffer_seconds * sample_rate)))
        self._pending_audio = bytearray()
        self._pending_spans: list[TimelineSpan] = []
        self._pending_last_segment_start: int | None = None
        self._pending_awaiting_physical_stop = False
        self._pending_physical_segment_ids: list[str] = []

    def _complete(self) -> list[SemanticEmission]:
        if not self._pending_spans or not self._pending_audio:
            self._pending_audio.clear()
            self._pending_spans.clear()
            self._pending_last_segment_start = None
            self._pending_awaiting_physical_stop = False
            self._pending_physical_segment_ids.clear()
            return []
        start = self._pending_spans[0].session_start_sample
        end = self._pending_spans[-1].session_end_sample
        last_segment_start = self._pending_last_segment_start
        awaiting_physical_stop = self._pending_awaiting_physical_stop
        physical_segment_ids = tuple(self._pending_physical_segment_ids)
        audio = bytes(self._pending_audio)
        spans = tuple(self._pending_spans)
        self._pending_audio.clear()
        self._pending_spans.clear()
        self._pending_last_segment_start = None
        self._pending_awaiting_physical_stop = False
        self._pending_physical_segment_ids.clear()
        return [
            (
                "segments",
                SegmentCandidate(
                    audio=audio,
                    start_sample=start,
                    end_sample=end,
                    turn_completed=True,
                    evaluate_turn=False,
                    timeline_spans=spans,
                    physical_segment_ids=physical_segment_ids,
                ),
            ),
            (
                "speech_stopped",
                SpeechStopped(
                    start_sample=start,
                    end_sample=end,
                    sample_rate=self._sample_rate,
                    last_segment_start_sample=last_segment_start,
                    awaiting_physical_stop=awaiting_physical_stop,
                    physical_segment_ids=physical_segment_ids,
                ),
            ),
        ]

    def _append_candidate_slice(
        self,
        candidate: SegmentCandidate,
        start_sample: int,
        end_sample: int,
    ) -> None:
        input_start = len(self._pending_audio) // 2
        self._pending_audio.extend(
            candidate.audio[start_sample * 2 : end_sample * 2]
        )
        spans = slice_timeline_spans(
            candidate_timeline_spans(candidate),
            start_sample,
            end_sample,
        )
        self._pending_spans.extend(
            TimelineSpan(
                input_start_sample=input_start + span.input_start_sample,
                input_end_sample=input_start + span.input_end_sample,
                session_start_sample=span.session_start_sample,
            )
            for span in spans
        )
        if spans:
            self._pending_last_segment_start = spans[0].session_start_sample
        for physical_segment_id in candidate_physical_segment_ids(candidate):
            if physical_segment_id not in self._pending_physical_segment_ids:
                self._pending_physical_segment_ids.append(physical_segment_id)
        self._pending_awaiting_physical_stop = (
            candidate.physical_segment_completed
            and end_sample == len(candidate.audio) // 2
        )

    async def accept(
        self,
        candidate: SegmentCandidate,
        *,
        failure_policy: FailurePolicy,
    ) -> list[SemanticEmission]:
        samples = len(candidate.audio) // 2
        result: list[SemanticEmission] = []
        if samples > self._max_samples:
            result.extend(self._complete())
            offset = 0
            while offset < samples:
                end = min(offset + self._max_samples, samples)
                self._append_candidate_slice(candidate, offset, end)
                result.extend(self._complete())
                offset = end
            return result
        if (
            self._pending_spans
            and len(self._pending_audio) // 2 + samples > self._max_samples
        ):
            result.extend(self._complete())
        self._append_candidate_slice(candidate, 0, samples)
        completed = not candidate.evaluate_turn
        if candidate.evaluate_turn:
            try:
                completed = await self._inferencer.detect_turn(
                    candidate.audio,
                    self._sample_rate,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if failure_policy is FailurePolicy.FAIL_PIPELINE:
                    raise
                logger.warning("语义 VAD 失败，按 fail_open 放行", exc_info=True)
                completed = True
        if completed:
            result.extend(self._complete())
        else:
            result.append(
                (
                    "turn_progress",
                    AsrInferencer.Result(
                        transcript="",
                        is_final=True,
                        start_sample=candidate.start_sample,
                        end_sample=candidate.end_sample,
                        sample_rate=self._sample_rate,
                        turn_completed=False,
                        physical_segment_ids=(
                            candidate_physical_segment_ids(candidate)
                        ),
                    ),
                )
            )
        return result

    def finish(self) -> list[SemanticEmission]:
        return self._complete()

    async def run(
        self,
        candidates: AsyncIterator[SegmentCandidate],
        *,
        failure_policy: FailurePolicy,
    ) -> AsyncIterator[SemanticEmission]:
        async for candidate in candidates:
            for emission in await self.accept(
                candidate,
                failure_policy=failure_policy,
            ):
                yield emission
        for emission in self.finish():
            yield emission
