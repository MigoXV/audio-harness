from __future__ import annotations

import math
import time
from unittest.mock import AsyncMock

import pytest

from audio_harness.configs import AppConfig
from audio_harness.vectors.qdrant import _python, _value, filter_message, point_id, point_id_text
from audio_harness.vectors.models import VOICEPRINT_PAYLOAD_SCHEMA, CollectionSchema, NamedVector
from audio_harness.vectors.service import VectorService, _validate_voiceprint_payload


class _QdrantCollections:
    def __init__(self) -> None:
        self.created: list[tuple[str, tuple[NamedVector, ...]]] = []
        self.deleted: list[str] = []
        self.indexes: list[tuple[str, dict[str, str]]] = []
        self.upserts: list[tuple[str, str, dict[str, tuple[float, ...]], dict]] = []
        self.replacements: list[tuple[str, str, dict]] = []

    async def list_collections(self) -> tuple[str, ...]:
        return ("voiceprints", "other")

    async def inspect_collection(self, name: str) -> CollectionSchema:
        if name == "other":
            raise ValueError("不是 Named Vector Collection")
        return CollectionSchema(
            vectors=(NamedVector("eres2netv2", 192, "cosine"),),
            payload_schema=VOICEPRINT_PAYLOAD_SCHEMA if name == "voiceprints" else {},
        )

    async def create_collection(self, name: str, vectors: tuple[NamedVector, ...]) -> None:
        self.created.append((name, vectors))

    async def create_payload_indexes(self, name: str, schema: dict[str, str]) -> None:
        self.indexes.append((name, schema))

    async def delete_collection(self, name: str) -> None:
        self.deleted.append(name)

    async def point_exists(self, _name: str, _point_id: str) -> bool:
        return False

    async def upsert(self, name: str, point_id: str, vectors: dict[str, tuple[float, ...]], payload: dict) -> None:
        self.upserts.append((name, point_id, vectors, payload))

    async def replace_payload(self, name: str, point_id: str, payload: dict) -> None:
        self.replacements.append((name, point_id, payload))


class _FailingIndexes(_QdrantCollections):
    async def create_payload_indexes(self, name: str, schema: dict[str, str]) -> None:
        raise RuntimeError("index failed")


def test_qdrant_payload_round_trip_preserves_json_and_integers() -> None:
    payload = {"name": "样本", "quality": 9, "active": True, "tags": ["a", None]}
    assert _python(_value(payload)) == payload


def test_qdrant_rest_filter_is_converted_to_grpc() -> None:
    result = filter_message({
        "must": [
            {"key": "channel", "match": {"value": "browser"}},
            {"key": "quality", "range": {"gte": 0.8}},
        ]
    })
    assert result is not None
    assert result.must[0].field.match.keyword == "browser"
    assert math.isclose(result.must[1].field.range.gte, 0.8)


def test_qdrant_point_id_supports_uuid_and_uint64() -> None:
    assert point_id_text(point_id(42)) == "42"
    assert point_id_text(point_id("f4b75fb5-4c8e-4af3-9c07-aa0ba8b9084a")) == "f4b75fb5-4c8e-4af3-9c07-aa0ba8b9084a"


def test_qdrant_environment_requires_both_endpoints(monkeypatch) -> None:
    monkeypatch.setenv("QDRANT_GRPC_TARGET", "127.0.0.1:6334")
    monkeypatch.delenv("QDRANT_HTTP_URL", raising=False)
    try:
        AppConfig.from_environment()
    except ValueError as exc:
        assert "必须同时配置" in str(exc)
    else:
        raise AssertionError("单独配置 Qdrant gRPC 地址应被拒绝")


@pytest.mark.asyncio
async def test_vector_spaces_are_read_directly_from_qdrant() -> None:
    service = VectorService(
        None,  # type: ignore[arg-type]
        _QdrantCollections(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )

    spaces = await service.list_spaces()

    assert [item.collection_name for item in spaces] == ["voiceprints", "other"]
    assert spaces[0].vectors == (NamedVector("eres2netv2", 192, "cosine"),)
    assert spaces[0].template == "voiceprint"
    assert spaces[1].vectors == ()


@pytest.mark.asyncio
async def test_voice_feature_template_creates_qdrant_payload_indexes() -> None:
    gateway = _QdrantCollections()
    service = VectorService(None, gateway, None, None)  # type: ignore[arg-type]

    created = await service.create_space({
        "collection_name": "voiceprints",
        "vectors": [{"name": "eres2netv2", "size": 192, "distance": "cosine"}],
        "template": "voiceprint",
    })

    assert created.template == "voiceprint"
    assert gateway.indexes == [("voiceprints", VOICEPRINT_PAYLOAD_SCHEMA)]


@pytest.mark.asyncio
async def test_voice_feature_template_rolls_back_partial_collection() -> None:
    gateway = _FailingIndexes()
    service = VectorService(None, gateway, None, None)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="index failed"):
        await service.create_space({
            "collection_name": "voiceprints",
            "vectors": [{"name": "eres2netv2", "size": 192, "distance": "cosine"}],
            "template": "voiceprint",
        })

    assert gateway.deleted == ["voiceprints"]


@pytest.mark.asyncio
async def test_voice_feature_registration_writes_required_metadata_defaults() -> None:
    gateway = _QdrantCollections()
    service = VectorService(None, gateway, None, None)  # type: ignore[arg-type]
    service.embed_audio = AsyncMock(return_value=(1.0,) * 192)  # type: ignore[method-assign]
    before = time.time_ns() // 1_000_000

    await service.register_audio(
        "voiceprints", b"audio",
        [{"vector_name": "eres2netv2", "engine_id": "embedding", "algorithm": "eres2netv2"}],
        {"source": "upload"}, "point-1", speaker_id=" speaker-1 ", channel_id=" microphone-1 ",
    )

    payload = gateway.upserts[0][3]
    assert payload["speaker_id"] == "speaker-1"
    assert payload["channel_id"] == "microphone-1"
    assert payload["quality_score"] == 0.0
    assert payload["cluster_id"] == -1
    assert payload["captured_at"] >= before
    assert payload["source"] == "upload"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"speaker_id": "", "channel_id": "c", "captured_at": 1, "quality_score": 0.5, "cluster_id": -1}, "speaker_id"),
        ({"speaker_id": "s", "channel_id": "c", "captured_at": -1, "quality_score": 0.5, "cluster_id": -1}, "captured_at"),
        ({"speaker_id": "s", "channel_id": "c", "captured_at": 1, "quality_score": 1.1, "cluster_id": -1}, "quality_score"),
        ({"speaker_id": "s", "channel_id": "c", "captured_at": 1, "quality_score": 0.5, "cluster_id": 1.2}, "cluster_id"),
    ],
)
def test_voice_feature_metadata_validation(values: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _validate_voiceprint_payload(values)


@pytest.mark.asyncio
async def test_voice_feature_rejects_reserved_extra_payload_and_incomplete_replace() -> None:
    gateway = _QdrantCollections()
    service = VectorService(None, gateway, None, None)  # type: ignore[arg-type]
    service.embed_audio = AsyncMock(return_value=(1.0,) * 192)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="保留字段"):
        await service.register_audio(
            "voiceprints", b"audio",
            [{"vector_name": "eres2netv2", "engine_id": "embedding", "algorithm": "eres2netv2"}],
            {"speaker_id": "duplicate"}, None, speaker_id="speaker-1", channel_id="channel-1",
        )
    with pytest.raises(ValueError, match="speaker_id"):
        await service.replace_payload("voiceprints", "point-1", {"custom": True})


@pytest.mark.asyncio
async def test_generic_collection_keeps_arbitrary_payload() -> None:
    gateway = _QdrantCollections()
    service = VectorService(None, gateway, None, None)  # type: ignore[arg-type]
    service.embed_audio = AsyncMock(return_value=(1.0,) * 192)  # type: ignore[method-assign]

    await service.register_audio(
        "generic", b"audio",
        [{"vector_name": "eres2netv2", "engine_id": "embedding", "algorithm": "eres2netv2"}],
        {"anything": [1, 2, 3]}, "point-1",
    )

    assert gateway.upserts[0][3] == {"anything": [1, 2, 3]}
