from __future__ import annotations

import math

import numpy as np
import pytest

from audio_harness.inferencers.embedding.postprocessor import (
    ClusteredSpeakerAudio,
    SpeakerAudioSegment,
    SpeakerClusteringPostprocessor,
)
from audio_harness.inferencers.embedding.base import EngineFeatureInferencer


def _pcm(value: int, samples: int = 1) -> bytes:
    return value.to_bytes(2, "little", signed=True) * samples


def _speaker(
    local_id: str,
    *segments: tuple[str, int, bytes],
) -> ClusteredSpeakerAudio:
    return ClusteredSpeakerAudio(
        local_speaker_id=local_id,
        segments=tuple(
            SpeakerAudioSegment(segment_id, start, audio)
            for segment_id, start, audio in segments
        ),
    )


class _Embedding(EngineFeatureInferencer):
    def __init__(self, vectors: dict[bytes, tuple[float, ...]]) -> None:
        self.vectors = vectors
        self.calls: list[tuple[bytes, bool]] = []

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass

    async def embed_bytes(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> bytes:
        assert algorithm == "voiceprint"
        self.calls.append((pcm_s16le, normalize))
        vector = np.asarray(self.vectors[pcm_s16le], dtype="<f4")
        assert len(vector) == expected_dimension
        return vector.tobytes()

    async def embed(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> tuple[float, ...]:
        raw = await self.embed_bytes(
            pcm_s16le,
            algorithm=algorithm,
            expected_dimension=expected_dimension,
            normalize=normalize,
        )
        return tuple(float(value) for value in np.frombuffer(raw, dtype="<f4"))

    def streaming_clustering(self):
        raise AssertionError("后处理测试不应启动流式聚类")


def _processor(
    embedding: _Embedding,
    *,
    high: float = 0.7,
    low: float = 0.5,
    max_audio_seconds: float = 20,
) -> SpeakerClusteringPostprocessor:
    dimension = len(next(iter(embedding.vectors.values())))
    return SpeakerClusteringPostprocessor(
        embedding,
        sample_rate=1,
        algorithm="voiceprint",
        expected_dimension=dimension,
        high_similarity_threshold=high,
        low_similarity_threshold=low,
        max_audio_seconds=max_audio_seconds,
    )


@pytest.mark.asyncio
async def test_keeps_valid_small_merge_when_chain_endpoint_is_dissimilar() -> None:
    embedding = _Embedding(
        {
            _pcm(1): (1, 0, 0),
            _pcm(2): (0.85, math.sqrt(0.2775), 0),
            _pcm(3): (
                0.4,
                (0.8 - 0.85 * 0.4) / math.sqrt(0.2775),
                math.sqrt(
                    1
                    - 0.4**2
                    - ((0.8 - 0.85 * 0.4) / math.sqrt(0.2775)) ** 2
                ),
            ),
        }
    )
    processor = _processor(embedding)
    speakers = (
        _speaker("a", ("a", 0, _pcm(1))),
        _speaker("b", ("b", 1, _pcm(2))),
        _speaker("c", ("c", 2, _pcm(3))),
    )

    assert await processor.process(speakers, (1, 2, 3)) == (1, 1, 3)
    assert all(normalize is True for _, normalize in embedding.calls)


@pytest.mark.asyncio
async def test_reuses_vectors_and_unchanged_similarity_matrix_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding = _Embedding(
        {
            _pcm(1): (1, 0, 0),
            _pcm(2): (0, 1, 0),
            _pcm(3): (0, 0, 1),
            _pcm(1) + _pcm(4): (1, 0, 0),
        }
    )
    processor = _processor(embedding)
    speakers = (
        _speaker("a", ("a", 0, _pcm(1))),
        _speaker("b", ("b", 1, _pcm(2))),
        _speaker("c", ("c", 2, _pcm(3))),
    )
    dot_calls = 0
    original_dot = np.dot

    def tracked_dot(left, right):
        nonlocal dot_calls
        dot_calls += 1
        return original_dot(left, right)

    monkeypatch.setattr(
        "audio_harness.inferencers.embedding.postprocessor.np.dot",
        tracked_dot,
    )

    await processor.process(speakers, (1, 2, 3))
    await processor.process(speakers, (1, 2, 3))
    changed = (
        _speaker("a", ("a", 0, _pcm(1)), ("a-2", 3, _pcm(4))),
        speakers[1],
        speakers[2],
    )
    await processor.process(changed, (1, 2, 3))

    assert len(embedding.calls) == 4
    assert dot_calls == 5


@pytest.mark.asyncio
async def test_orders_audio_and_limits_each_profile_to_first_twenty_seconds() -> None:
    first_twenty = _pcm(1, 10) + _pcm(2, 10)
    embedding = _Embedding(
        {
            first_twenty: (1, 0),
            _pcm(9): (0, 1),
        }
    )
    processor = _processor(embedding)
    speakers = (
        _speaker(
            "a",
            ("late", 30, _pcm(3, 10)),
            ("middle", 20, _pcm(2, 10)),
            ("early", 10, _pcm(1, 10)),
        ),
        _speaker("b", ("b", 40, _pcm(9))),
    )

    await processor.process(speakers, (1, 2))

    assert embedding.calls[0][0] == first_twenty


@pytest.mark.asyncio
async def test_prefers_non_negative_id_then_earliest_negative_id() -> None:
    vectors = {_pcm(1): (1, 0), _pcm(2): (1, 0)}
    speakers = (
        _speaker("late", ("late", 10, _pcm(1))),
        _speaker("early", ("early", 0, _pcm(2))),
    )

    assert await _processor(_Embedding(vectors)).process(
        speakers,
        (-2, 5),
    ) == (5, 5)
    assert await _processor(_Embedding(vectors)).process(
        speakers,
        (-1, -2),
    ) == (-2, -2)
