from __future__ import annotations

import base64
import math
import time
import wave
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from fastapi import FastAPI

from audio_harness.api.routers.vectors import create_vector_router
from audio_harness.configs.app import AppConfig, QdrantConfig
from audio_harness.engines.models import EngineApi, EngineKind
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.protos.qdrant import points_pb2, qdrant_common_pb2
from audio_harness.vectors.qdrant import QdrantGateway, _python, _value, filter_message, point_id, point_id_text
from audio_harness.vectors.models import (
    VOICEPRINT_PAYLOAD_SCHEMA,
    CollectionSchema,
    NamedVector,
    SpeakerMatch,
    VectorObservation,
)
from audio_harness.vectors.service import VectorService, _validate_voiceprint_payload
from audio_harness.vectors.vad_preview import (
    VadBoundaryCollector,
    concatenate_intervals,
    merge_intervals,
)


class _QdrantCollections:
    def __init__(self) -> None:
        self.created: list[tuple[str, tuple[NamedVector, ...]]] = []
        self.deleted: list[str] = []
        self.indexes: list[tuple[str, dict[str, str]]] = []
        self.upserts: list[tuple[str, str, dict[str, tuple[float, ...]], dict]] = []
        self.replacements: list[tuple[str, str, dict]] = []
        self.searches: list[tuple[str, str, tuple[float, ...], int, float | None, dict | None]] = []

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

    async def search_speakers(self, name: str, vector_name: str, vector: tuple[float, ...], *, limit: int, score_threshold: float | None, filter_json: dict | None) -> tuple[SpeakerMatch, ...]:
        self.searches.append((name, vector_name, vector, limit, score_threshold, filter_json))
        return (SpeakerMatch("speaker-1", 0.95, VectorObservation("point-1", {"speaker_id": "speaker-1"})),)


class _FailingIndexes(_QdrantCollections):
    async def create_payload_indexes(self, name: str, schema: dict[str, str]) -> None:
        raise RuntimeError("index failed")


class _Vad:
    def __init__(self, updates: tuple[VadInferencer.Update, ...]) -> None:
        self.updates = updates
        self.audio = b""
        self.sample_rate = 0
        self.postprocessing = None

    def stream_detect(self, chunks, sample_rate: int, *, postprocessing=None):
        async def iterate():
            self.sample_rate = sample_rate
            self.postprocessing = postprocessing
            self.audio = b"".join([chunk async for chunk in chunks])
            for update in self.updates:
                yield update

        return iterate()


class _Pool:
    def __init__(self, vad: _Vad) -> None:
        self.vad = vad
        self.released = False

    @asynccontextmanager
    async def lease(self, snapshots):
        snapshot = tuple(snapshots)[0]
        try:
            yield {(snapshot.id, snapshot.revision): self.vad}
        finally:
            self.released = True


class _Catalog:
    def __init__(self, *, enabled: bool = True, kind=EngineKind.VAD) -> None:
        self.asset = SimpleNamespace(
            id="vad-1",
            revision=1,
            enabled=enabled,
            kind=kind,
            api=EngineApi.UX_VAD,
        )
        self.asset.snapshot = lambda: self.asset

    async def get_asset(self, asset_id: str):
        assert asset_id == "vad-1"
        return self.asset


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


def test_vad_boundaries_are_clamped_merged_and_finished() -> None:
    collector = VadBoundaryCollector(total_samples=10)
    collector.accept(VadInferencer.Update(((1, 4),), 4))
    collector.accept(VadInferencer.Update(((3, 7),), 7))
    collector.accept(VadInferencer.Update(((8, None),), 10))

    assert collector.finish() == ((1, 7), (8, 10))
    assert merge_intervals(((5, 9), (-2, 2), (2, 4))) == ((-2, 4), (5, 9))
    pcm = b"".join(value.to_bytes(2, "little", signed=True) for value in range(10))
    assert concatenate_intervals(pcm, ((1, 3), (7, 9))) == (
        pcm[2:6] + pcm[14:18]
    )


@pytest.mark.asyncio
async def test_vad_preview_returns_the_exact_concatenated_wav(monkeypatch) -> None:
    pcm = b"".join(value.to_bytes(2, "little", signed=True) for value in range(10))
    vad = _Vad((VadInferencer.Update(((1, 4), (6, 9)), 10),))
    pool = _Pool(vad)
    service = VectorService(
        None,  # type: ignore[arg-type]
        _QdrantCollections(),  # type: ignore[arg-type]
        _Catalog(),  # type: ignore[arg-type]
        pool,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "audio_harness.vectors.service.decode_audio",
        AsyncMock(return_value=pcm),
    )

    wav, duration_ms = await service.preview_vad_audio(
        "voiceprints",
        b"uploaded-audio",
        vad_engine_id="vad-1",
    )

    with wave.open(BytesIO(wav), "rb") as reader:
        assert reader.getframerate() == 16_000
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.readframes(reader.getnframes()) == pcm[2:8] + pcm[12:18]
    assert duration_ms == 1
    assert vad.audio == pcm
    assert vad.sample_rate == 16_000
    assert pool.released is True


@pytest.mark.asyncio
async def test_vad_preview_rejects_missing_speech_and_invalid_engine(monkeypatch) -> None:
    monkeypatch.setattr(
        "audio_harness.vectors.service.decode_audio",
        AsyncMock(return_value=b"\0\0" * 160),
    )
    no_speech = VectorService(
        None,  # type: ignore[arg-type]
        _QdrantCollections(),  # type: ignore[arg-type]
        _Catalog(),  # type: ignore[arg-type]
        _Pool(_Vad(())),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="未检测到有效语音"):
        await no_speech.preview_vad_audio(
            "voiceprints", b"audio", vad_engine_id="vad-1"
        )

    invalid = VectorService(
        None,  # type: ignore[arg-type]
        _QdrantCollections(),  # type: ignore[arg-type]
        _Catalog(kind=EngineKind.ASR),  # type: ignore[arg-type]
        _Pool(_Vad(())),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="已启用的 VAD"):
        await invalid.preview_vad_audio(
            "voiceprints", b"audio", vad_engine_id="vad-1"
        )


@pytest.mark.asyncio
async def test_qdrant_speaker_search_uses_query_groups() -> None:
    class _Points:
        request: points_pb2.QueryPointGroups | None = None

        async def QueryGroups(self, request: points_pb2.QueryPointGroups):
            self.request = request
            return points_pb2.QueryGroupsResponse(result=points_pb2.GroupsResult(groups=[
                points_pb2.PointGroup(
                    id=points_pb2.GroupId(string_value="speaker-1"),
                    hits=[points_pb2.ScoredPoint(
                        id=qdrant_common_pb2.PointId(uuid="f4b75fb5-4c8e-4af3-9c07-aa0ba8b9084a"),
                        score=0.95,
                        payload={"speaker_id": _value("speaker-1")},
                    )],
                ),
            ]))

    gateway = QdrantGateway("unused:6334")
    points = _Points()
    gateway._points = points  # type: ignore[assignment]
    try:
        groups = await gateway.search_speakers(
            "voiceprints", "eres2netv2", (1.0, 0.0), limit=5,
            score_threshold=0.8,
            filter_json={"must": [{"key": "channel_id", "match": {"value": "mic-1"}}]},
        )
    finally:
        await gateway.close()

    assert points.request is not None
    assert points.request.group_by == "speaker_id"
    assert points.request.group_size == 1
    assert points.request.limit == 5
    assert points.request.using == "eres2netv2"
    assert tuple(points.request.query.nearest.dense.data) == (1.0, 0.0)
    assert len(groups) == 1
    assert groups[0].speaker_id == "speaker-1"
    assert math.isclose(groups[0].score, 0.95, abs_tol=1e-6)
    assert groups[0].observation == VectorObservation(
        "f4b75fb5-4c8e-4af3-9c07-aa0ba8b9084a", {"speaker_id": "speaker-1"},
    )


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
    assert payload["captured_at"] >= before
    assert payload["source"] == "upload"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"speaker_id": "", "channel_id": "c", "captured_at": 1, "quality_score": 0.5}, "speaker_id"),
        ({"speaker_id": "s", "channel_id": "c", "captured_at": -1, "quality_score": 0.5}, "captured_at"),
        ({"speaker_id": "s", "channel_id": "c", "captured_at": 1, "quality_score": 1.1}, "quality_score"),
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


@pytest.mark.asyncio
async def test_voiceprint_search_returns_one_observation_per_speaker() -> None:
    gateway = _QdrantCollections()
    service = VectorService(None, gateway, None, None)  # type: ignore[arg-type]
    service.embed_audio = AsyncMock(return_value=(1.0,) * 192)  # type: ignore[method-assign]

    vector, groups = await service.search_audio(
        "voiceprints", b"audio",
        vector_name="eres2netv2", engine_id="embedding", algorithm="eres2netv2",
        normalize=True, top_k=7, score_threshold=0.8,
        filter_json={"must": [{"key": "channel_id", "match": {"value": "mic-1"}}]},
    )

    assert vector == (1.0,) * 192
    assert groups[0].speaker_id == "speaker-1"
    assert groups[0].score == 0.95
    assert groups[0].observation.point_id == "point-1"
    assert gateway.searches == [(
        "voiceprints", "eres2netv2", (1.0,) * 192, 7, 0.8,
        {"must": [{"key": "channel_id", "match": {"value": "mic-1"}}]},
    )]


@pytest.mark.asyncio
async def test_audio_speaker_search_rejects_non_voiceprint_collection() -> None:
    gateway = _QdrantCollections()
    service = VectorService(None, gateway, None, None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="仅支持 voiceprint"):
        await service.search_embedding(
            "generic", (1.0,) * 192, vector_name="eres2netv2", top_k=10,
            score_threshold=None, filter_json=None,
        )


@pytest.mark.asyncio
async def test_audio_search_api_returns_domain_speaker_groups() -> None:
    class _Service:
        async def search_audio(self, *_args, **_kwargs):
            return (1.0, 0.0), (SpeakerMatch(
                "speaker-1", 0.95,
                VectorObservation("point-1", {"speaker_id": "speaker-1"}),
            ),)

    app = FastAPI()
    app.include_router(create_vector_router(_Service(), QdrantConfig()))  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/vector-spaces/voiceprints/search/audio",
            files={"audio": ("sample.wav", b"audio", "audio/wav")},
            data={
                "vector_name": "eres2netv2", "engine_id": "embedding",
                "algorithm": "eres2netv2", "top_k": "10", "filter_json": "{}",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "embedding": [1.0, 0.0],
        "groups": [{
            "speaker_id": "speaker-1",
            "score": 0.95,
            "observation": {
                "point_id": "point-1",
                "payload": {"speaker_id": "speaker-1"},
            },
        }],
    }


@pytest.mark.asyncio
async def test_vad_preview_api_returns_base64_wav() -> None:
    class _Service:
        async def preview_vad_audio(self, *_args, **_kwargs):
            assert _kwargs == {"vad_engine_id": "vad-1"}
            return b"RIFF-preview", 1250

    app = FastAPI()
    app.include_router(create_vector_router(_Service(), QdrantConfig()))  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/vector-spaces/voiceprints/search/audio/vad-preview",
            files={"audio": ("sample.wav", b"audio", "audio/wav")},
            data={"vad_engine_id": "vad-1"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "audio_base64": base64.b64encode(b"RIFF-preview").decode("ascii"),
        "content_type": "audio/wav",
        "duration_ms": 1250,
    }
