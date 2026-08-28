from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

from audio_harness.inferencers.embedding.base import (
    EngineFeatureInferencer,
    StreamingClusteringSession,
)
from audio_harness.inferencers.embedding.postprocessor import (
    ClusteredSpeakerAudio,
    MAX_PROFILE_AUDIO_SECONDS,
    SpeakerAudioSegment,
    SpeakerClusteringPostprocessor,
)
from audio_harness.sessions.contracts import (
    DiarizationContext,
    SegmentCandidate,
)
from audio_harness.workflows.events import (
    DiarizationClusterAssignment,
    DiarizationClusterResult,
)
from audio_harness.workflows.models import (
    FailurePolicy,
    SpeakerClusteringNodeConfig,
)

logger = logging.getLogger(__name__)

ClusteringEmission = tuple[str, DiarizationClusterResult]


@dataclass(frozen=True)
class _ClusterVector:
    local_speaker_id: str
    segment_ids: tuple[str, ...]
    audio: ClusteredSpeakerAudio
    vector: bytes


class SpeakerClusteringSession:
    """跨本地分离批次维护说话人向量与全量聚类快照。"""

    def __init__(
        self,
        config: SpeakerClusteringNodeConfig,
        sample_rate: int,
        inferencer: EngineFeatureInferencer,
        *,
        failure_policy: FailurePolicy,
        embedding_limiter: Any | None = None,
    ) -> None:
        self._config = config
        self._inferencer = inferencer
        self._failure_policy = failure_policy
        self._embedding_limiter = embedding_limiter
        self._max_embedding_samples = max(
            1,
            int(round(config.max_embedding_seconds * sample_rate)),
        )
        self._max_postprocessing_audio_bytes = max(
            2,
            int(round(MAX_PROFILE_AUDIO_SECONDS * sample_rate)) * 2,
        )
        self._vectors: list[_ClusterVector] = []
        self._failed_assignments: list[DiarizationClusterAssignment] = []
        self._pending: list[SegmentCandidate] = []
        self._engine_session: StreamingClusteringSession | None = None
        self._revision = 0
        self._last_completed_revision: int | None = None
        self._last_labels: tuple[int, ...] = ()
        self._fallback_speaker_ids: dict[str, int] = {}
        self._next_fallback_speaker_id = -1
        postprocessing = config.postprocessing
        self._postprocessor = (
            SpeakerClusteringPostprocessor(
                inferencer,
                sample_rate=sample_rate,
                algorithm=config.embedding_algorithm,
                expected_dimension=config.expected_dimension,
                high_similarity_threshold=(
                    postprocessing.high_similarity_threshold
                ),
                low_similarity_threshold=(
                    postprocessing.low_similarity_threshold
                ),
                embedding_limiter=embedding_limiter,
            )
            if postprocessing is not None
            else None
        )

    def _fallback_speaker_id(self, local_speaker_id: str) -> int:
        if local_speaker_id not in self._fallback_speaker_ids:
            self._fallback_speaker_ids[local_speaker_id] = (
                self._next_fallback_speaker_id
            )
            self._next_fallback_speaker_id -= 1
        return self._fallback_speaker_ids[local_speaker_id]

    def _effective_speaker_ids(
        self,
        labels: tuple[int, ...],
    ) -> tuple[int, ...]:
        if len(labels) != len(self._vectors):
            raise ValueError(
                "流式聚类返回标签数量不符: "
                f"expected={len(self._vectors)}, actual={len(labels)}"
            )
        return tuple(
            label
            if label >= 0
            else self._fallback_speaker_id(record.local_speaker_id)
            for record, label in zip(self._vectors, labels, strict=True)
        )

    def _assignments(
        self,
        speaker_ids: tuple[int, ...],
    ) -> tuple[DiarizationClusterAssignment, ...]:
        if len(speaker_ids) != len(self._vectors):
            raise ValueError(
                "聚类 speaker ID 数量不符: "
                f"expected={len(self._vectors)}, actual={len(speaker_ids)}"
            )
        values = list(self._failed_assignments)
        for record, speaker_id in zip(
            self._vectors,
            speaker_ids,
            strict=True,
        ):
            values.extend(
                DiarizationClusterAssignment(
                    segment_id=segment_id,
                    local_speaker_id=record.local_speaker_id,
                    speaker_id=speaker_id,
                )
                for segment_id in record.segment_ids
            )
        return tuple(values)

    async def _embed(self, audio: bytes) -> bytes:
        kwargs = {
            "algorithm": self._config.embedding_algorithm,
            "expected_dimension": self._config.expected_dimension,
            "normalize": self._config.normalize,
        }
        if self._embedding_limiter is None:
            return await self._inferencer.embed_bytes(audio, **kwargs)
        async with self._embedding_limiter:
            return await self._inferencer.embed_bytes(audio, **kwargs)

    async def _process_batch(
        self,
        candidates: tuple[SegmentCandidate, ...],
    ) -> AsyncIterator[ClusteringEmission]:
        grouped: dict[str, list[SegmentCandidate]] = {}
        for candidate in candidates:
            context = candidate.diarization
            if context is None or context.local_speaker_id is None:
                continue
            grouped.setdefault(context.local_speaker_id, []).append(candidate)
        if not grouped:
            return

        self._revision += 1
        new_vectors: list[_ClusterVector] = []
        for local_speaker_id, pieces in grouped.items():
            pieces.sort(key=lambda value: (value.start_sample, value.end_sample))
            segment_ids = tuple(
                cast(DiarizationContext, piece.diarization).segment_id
                for piece in pieces
            )
            postprocessing_segments: list[SpeakerAudioSegment] = []
            remaining_audio_bytes = self._max_postprocessing_audio_bytes
            if self._postprocessor is not None:
                for piece in pieces:
                    if remaining_audio_bytes <= 0:
                        break
                    piece_audio = piece.audio[:remaining_audio_bytes]
                    if piece_audio:
                        postprocessing_segments.append(
                            SpeakerAudioSegment(
                                segment_id=cast(
                                    DiarizationContext,
                                    piece.diarization,
                                ).segment_id,
                                start_sample=piece.start_sample,
                                pcm_s16le=piece_audio,
                            )
                        )
                        remaining_audio_bytes -= len(piece_audio)
            clustered_audio = ClusteredSpeakerAudio(
                local_speaker_id=local_speaker_id,
                segments=tuple(postprocessing_segments),
            )
            audio = b"".join(piece.audio for piece in pieces)
            audio = audio[: self._max_embedding_samples * 2]
            try:
                vector = await self._embed(audio)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "说话人聚类向量提取失败，片段暂未归档: %s",
                    exc,
                )
                self._failed_assignments.extend(
                    DiarizationClusterAssignment(
                        segment_id=segment_id,
                        local_speaker_id=local_speaker_id,
                        speaker_id=self._fallback_speaker_id(local_speaker_id),
                    )
                    for segment_id in segment_ids
                )
                continue
            new_vectors.append(
                _ClusterVector(
                    local_speaker_id=local_speaker_id,
                    segment_ids=segment_ids,
                    audio=clustered_audio,
                    vector=vector,
                )
            )

        if len(self._vectors) + len(new_vectors) > self._config.max_vectors:
            error = ValueError(
                f"说话人聚类向量数量超过上限 {self._config.max_vectors}"
            )
            yield (
                "results",
                DiarizationClusterResult(
                    phase="failed",
                    revision=self._revision,
                    replaces_revision=self._last_completed_revision,
                    assignments=(),
                    error_code="diarization_cluster_failed",
                    error_message=str(error),
                ),
            )
            if self._failure_policy is FailurePolicy.FAIL_PIPELINE:
                raise error
            return

        self._vectors.extend(new_vectors)
        started = time.monotonic()
        try:
            if len(self._vectors) < 2:
                labels = tuple(-1 for _ in self._vectors)
            elif self._engine_session is None:
                self._engine_session = self._inferencer.streaming_clustering()
                labels = await self._engine_session.start(
                    tuple(item.vector for item in self._vectors),
                    algorithm=self._config.clustering_algorithm,
                    parameters=self._config.parameters.model_dump(mode="json"),
                )
            elif new_vectors:
                labels = await self._engine_session.add(
                    tuple(item.vector for item in new_vectors)
                )
            else:
                labels = self._last_labels
            speaker_ids = self._effective_speaker_ids(labels)
            if self._postprocessor is not None:
                try:
                    speaker_ids = await self._postprocessor.process(
                        tuple(item.audio for item in self._vectors),
                        speaker_ids,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._failure_policy is FailurePolicy.FAIL_PIPELINE:
                        raise
                    logger.warning(
                        "说话人聚类后处理失败，使用原始聚类结果: %s",
                        exc,
                    )
            snapshot = self._assignments(speaker_ids)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("说话人流式聚类失败，保留上一版结果: %s", exc)
            if self._engine_session is not None:
                await self._engine_session.close()
                self._engine_session = None
            yield (
                "results",
                DiarizationClusterResult(
                    phase="failed",
                    revision=self._revision,
                    replaces_revision=self._last_completed_revision,
                    assignments=(),
                    error_code="diarization_cluster_failed",
                    error_message=str(exc),
                ),
            )
            if self._failure_policy is FailurePolicy.FAIL_PIPELINE:
                raise
            return

        yield (
            "results",
            DiarizationClusterResult(
                phase="completed",
                revision=self._revision,
                replaces_revision=self._last_completed_revision,
                assignments=snapshot,
            ),
        )
        self._last_completed_revision = self._revision
        self._last_labels = labels
        logger.debug(
            "说话人聚类完成: revision=%s vectors=%s latency_ms=%.2f",
            self._revision,
            len(self._vectors),
            (time.monotonic() - started) * 1000,
        )

    async def run(
        self,
        candidates: AsyncIterator[SegmentCandidate],
    ) -> AsyncIterator[ClusteringEmission]:
        try:
            async for candidate in candidates:
                context = candidate.diarization
                if context is None:
                    raise ValueError("说话人聚类节点只能接收说话人分离片段")
                self._pending.append(candidate)
                if context.batch_completed:
                    batch_id = context.batch_id
                    batch = tuple(
                        value
                        for value in self._pending
                        if cast(DiarizationContext, value.diarization).batch_id
                        == batch_id
                    )
                    self._pending = [
                        value
                        for value in self._pending
                        if cast(DiarizationContext, value.diarization).batch_id
                        != batch_id
                    ]
                    async for emission in self._process_batch(batch):
                        yield emission
        finally:
            if self._engine_session is not None:
                await self._engine_session.close()
