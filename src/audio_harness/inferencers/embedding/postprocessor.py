from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np

from audio_harness.inferencers.embedding.base import EngineFeatureInferencer


@dataclass(frozen=True)
class SpeakerAudioSegment:
    segment_id: str
    start_sample: int
    pcm_s16le: bytes


@dataclass(frozen=True)
class ClusteredSpeakerAudio:
    local_speaker_id: str
    segments: tuple[SpeakerAudioSegment, ...]


ProfileSignature = tuple[str, ...]
SimilarityKey = tuple[ProfileSignature, ProfileSignature]
MAX_PROFILE_AUDIO_SECONDS = 20.0


@dataclass(frozen=True)
class _Profile:
    speaker_id: int
    signature: ProfileSignature
    first_start_sample: int
    pcm_s16le: bytes


class SpeakerClusteringPostprocessor:
    """按全局 speaker 聚合音频，并用完整链接约束合并相似身份。"""

    def __init__(
        self,
        inferencer: EngineFeatureInferencer,
        *,
        sample_rate: int,
        algorithm: str,
        expected_dimension: int,
        high_similarity_threshold: float,
        low_similarity_threshold: float,
        embedding_limiter: Any | None = None,
        max_audio_seconds: float = MAX_PROFILE_AUDIO_SECONDS,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate 必须大于 0")
        if expected_dimension <= 0:
            raise ValueError("expected_dimension 必须大于 0")
        if not 0 <= low_similarity_threshold <= high_similarity_threshold <= 1:
            raise ValueError("聚类后处理阈值必须满足 0 <= low <= high <= 1")
        if max_audio_seconds <= 0:
            raise ValueError("max_audio_seconds 必须大于 0")
        self._inferencer = inferencer
        self._algorithm = algorithm
        self._expected_dimension = expected_dimension
        self._high_threshold = high_similarity_threshold
        self._low_threshold = low_similarity_threshold
        self._embedding_limiter = embedding_limiter
        self._max_audio_bytes = max(
            2,
            int(round(max_audio_seconds * sample_rate)) * 2,
        )
        self._vectors: dict[ProfileSignature, np.ndarray] = {}
        self._similarities: dict[SimilarityKey, float] = {}

    @staticmethod
    def _similarity_key(
        left: ProfileSignature,
        right: ProfileSignature,
    ) -> SimilarityKey:
        return (left, right) if left <= right else (right, left)

    def _profiles(
        self,
        speakers: tuple[ClusteredSpeakerAudio, ...],
        speaker_ids: tuple[int, ...],
    ) -> tuple[_Profile, ...]:
        if len(speakers) != len(speaker_ids):
            raise ValueError(
                "聚类后处理输入数量不符: "
                f"speakers={len(speakers)}, labels={len(speaker_ids)}"
            )
        grouped: dict[int, list[SpeakerAudioSegment]] = {}
        for speaker, speaker_id in zip(speakers, speaker_ids, strict=True):
            grouped.setdefault(speaker_id, []).extend(speaker.segments)

        profiles: list[_Profile] = []
        for speaker_id, segments in grouped.items():
            ordered = sorted(
                segments,
                key=lambda value: (value.start_sample, value.segment_id),
            )
            signature = tuple(value.segment_id for value in ordered)
            if len(signature) != len(set(signature)):
                raise ValueError("聚类后处理不能重复包含同一 segment_id")
            audio = bytearray()
            for segment in ordered:
                if not segment.pcm_s16le or len(segment.pcm_s16le) % 2:
                    raise ValueError("聚类后处理音频必须是非空 PCM S16LE")
                remaining = self._max_audio_bytes - len(audio)
                if remaining <= 0:
                    break
                audio.extend(segment.pcm_s16le[:remaining])
            profiles.append(
                _Profile(
                    speaker_id=speaker_id,
                    signature=signature,
                    first_start_sample=ordered[0].start_sample,
                    pcm_s16le=bytes(audio),
                )
            )
        return tuple(profiles)

    async def _embed(self, audio: bytes) -> np.ndarray:
        kwargs = {
            "algorithm": self._algorithm,
            "expected_dimension": self._expected_dimension,
            "normalize": True,
        }
        if self._embedding_limiter is None:
            raw = await self._inferencer.embed_bytes(audio, **kwargs)
        else:
            async with self._embedding_limiter:
                raw = await self._inferencer.embed_bytes(audio, **kwargs)
        if len(raw) != self._expected_dimension * 4:
            raise ValueError(
                "聚类后处理 embedding 维度不符: "
                f"expected={self._expected_dimension}, bytes={len(raw)}"
            )
        vector = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=True)
        if not np.isfinite(vector).all():
            raise ValueError("聚类后处理 embedding 包含 NaN 或 Infinity")
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            raise ValueError("聚类后处理 embedding 范数为 0")
        vector /= norm
        return vector

    @staticmethod
    def _canonical_speaker_id(
        members: Iterable[int],
        profiles_by_id: dict[int, _Profile],
    ) -> int:
        values = tuple(members)
        non_negative = tuple(value for value in values if value >= 0)
        if non_negative:
            return min(non_negative)
        return min(
            values,
            key=lambda value: (
                profiles_by_id[value].first_start_sample,
                value,
            ),
        )

    async def process(
        self,
        speakers: tuple[ClusteredSpeakerAudio, ...],
        speaker_ids: tuple[int, ...],
    ) -> tuple[int, ...]:
        if not speakers:
            self._vectors = {}
            self._similarities = {}
            return speaker_ids

        profiles = self._profiles(speakers, speaker_ids)
        if len(profiles) < 2:
            self._vectors = {}
            self._similarities = {}
            return speaker_ids
        vectors: dict[ProfileSignature, np.ndarray] = {}
        for profile in profiles:
            vector = self._vectors.get(profile.signature)
            if vector is None:
                vector = await self._embed(profile.pcm_s16le)
            vectors[profile.signature] = vector

        similarities: dict[SimilarityKey, float] = {}
        edges: list[tuple[float, int, int]] = []
        for left_index, left in enumerate(profiles):
            for right in profiles[left_index + 1 :]:
                key = self._similarity_key(left.signature, right.signature)
                similarity = self._similarities.get(key)
                if similarity is None:
                    similarity = float(
                        np.clip(
                            np.dot(
                                vectors[left.signature],
                                vectors[right.signature],
                            ),
                            -1,
                            1,
                        )
                    )
                similarities[key] = similarity
                if similarity >= self._high_threshold:
                    edges.append((similarity, left.speaker_id, right.speaker_id))

        profiles_by_id = {profile.speaker_id: profile for profile in profiles}
        groups = {profile.speaker_id: {profile.speaker_id} for profile in profiles}
        owner = {profile.speaker_id: profile.speaker_id for profile in profiles}
        edges.sort(key=lambda value: (-value[0], value[1], value[2]))
        for _, left_id, right_id in edges:
            left_owner = owner[left_id]
            right_owner = owner[right_id]
            if left_owner == right_owner:
                continue
            left_group = groups[left_owner]
            right_group = groups[right_owner]
            if any(
                similarities[
                    self._similarity_key(
                        profiles_by_id[left].signature,
                        profiles_by_id[right].signature,
                    )
                ]
                < self._low_threshold
                for left in left_group
                for right in right_group
            ):
                continue
            merged = left_group | right_group
            new_owner = self._canonical_speaker_id(merged, profiles_by_id)
            groups.pop(left_owner)
            groups.pop(right_owner)
            groups[new_owner] = merged
            for member in merged:
                owner[member] = new_owner

        remapped = {
            member: self._canonical_speaker_id(group, profiles_by_id)
            for group in groups.values()
            for member in group
        }
        self._vectors = vectors
        self._similarities = similarities
        return tuple(remapped[value] for value in speaker_ids)
