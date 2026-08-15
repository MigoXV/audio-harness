from __future__ import annotations

import math
import time
import uuid
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.database.models import AudioWorkflow, AudioWorkflowRevision
from audio_harness.engines.catalog import EngineCatalog
from audio_harness.engines.models import EngineApi, EngineKind
from audio_harness.engines.pool import InferencerPool
from audio_harness.inferencers.embedding import AudioEmbeddingInferencer
from audio_harness.inferencers.limits import ResizableConcurrencyLimiter
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.media.decode import decode_audio
from audio_harness.vectors.models import (
    VOICEPRINT_PAYLOAD_SCHEMA,
    NamedVector,
    SpeakerMatch,
    VectorSpaceData,
)
from audio_harness.vectors.qdrant import QdrantCollectionNotFoundError, QdrantGateway
from audio_harness.vectors.vad_preview import (
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    VadBoundaryCollector,
    concatenate_intervals,
    encode_wav,
    pcm_chunks,
)


class VectorNotFoundError(LookupError): pass
class VectorConflictError(RuntimeError): pass


def _validate_voiceprint_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    for field in ("speaker_id", "channel_id"):
        value = result.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} 必须是非空字符串")
        value = value.strip()
        if len(value) > 128:
            raise ValueError(f"{field} 最长为 128 个字符")
        result[field] = value
    captured_at = result.get("captured_at")
    if isinstance(captured_at, bool) or not isinstance(captured_at, int) or captured_at < 0:
        raise ValueError("captured_at 必须是非负 Unix 毫秒整数")
    quality = result.get("quality_score")
    if isinstance(quality, bool) or not isinstance(quality, (int, float)):
        raise ValueError("quality_score 必须是 0 到 1 的有限数值")
    quality = float(quality)
    if not math.isfinite(quality) or not 0 <= quality <= 1:
        raise ValueError("quality_score 必须是 0 到 1 的有限数值")
    result["quality_score"] = quality
    return result


class VectorService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], gateway: QdrantGateway, catalog: EngineCatalog, pool: InferencerPool, *, embedding_limiter: ResizableConcurrencyLimiter | None = None, qdrant_limiter: ResizableConcurrencyLimiter | None = None) -> None:
        self._sessions = sessions
        self.gateway = gateway
        self._catalog = catalog
        self._pool = pool
        self._embedding_limiter = embedding_limiter
        self._qdrant_limiter = qdrant_limiter

    async def infrastructure(self) -> tuple[bool, bool]:
        configured = self.gateway.target is not None
        return configured, await self.gateway.ready() if configured else False

    async def list_spaces(self) -> tuple[VectorSpaceData, ...]:
        spaces: list[VectorSpaceData] = []
        for collection_name in await self.gateway.list_collections():
            try:
                spaces.append(await self.get_space(collection_name))
            except VectorNotFoundError:
                continue
            except ValueError:
                spaces.append(VectorSpaceData(collection_name=collection_name, vectors=(), template=None))
        return tuple(spaces)

    async def get_space(self, collection_name: str) -> VectorSpaceData:
        try:
            schema = await self.gateway.inspect_collection(collection_name)
        except QdrantCollectionNotFoundError as exc:
            raise VectorNotFoundError(str(exc)) from exc
        template = (
            "voiceprint"
            if all(schema.payload_schema.get(key) == value for key, value in VOICEPRINT_PAYLOAD_SCHEMA.items())
            else None
        )
        return VectorSpaceData(collection_name=collection_name, vectors=schema.vectors, template=template)

    async def create_space(self, payload: dict[str, Any]) -> VectorSpaceData:
        vectors = tuple(NamedVector(**item) for item in payload.pop("vectors"))
        template = payload.pop("template", None)
        collection_name = payload["collection_name"]
        await self.gateway.create_collection(collection_name, vectors)
        try:
            if template == "voiceprint":
                await self.gateway.create_payload_indexes(collection_name, VOICEPRINT_PAYLOAD_SCHEMA)
        except Exception:
            await self.gateway.delete_collection(collection_name)
            raise
        return await self.get_space(collection_name)

    async def destroy(self, collection_name: str, confirmation: str) -> None:
        if confirmation != collection_name:
            raise ValueError("确认文本必须与 Collection 名完全一致")
        await self.get_space(collection_name)
        async with self._sessions() as session:
            drafts = (await session.scalars(select(AudioWorkflow))).all()
            revisions = (await session.scalars(select(AudioWorkflowRevision))).all()
            documents = [item.draft_document for item in drafts] + [item.document for item in revisions]
            if any(
                node.get("kind") == "vector_search"
                and (node.get("config") or {}).get("collection_name") == collection_name
                for document in documents
                for node in document.get("nodes", [])
            ):
                raise VectorConflictError("Collection 仍被语音工作流引用")
        await self.gateway.delete_collection(collection_name)

    async def embed_audio(self, audio: bytes, *, engine_id: str, algorithm: str, dimension: int, normalize: bool) -> tuple[float, ...]:
        pcm = await decode_audio(audio)
        asset = await self._catalog.get_asset(engine_id)
        if asset.api is not EngineApi.ENGINE_FEATURE or not asset.enabled:
            raise ValueError("所选引擎不是已启用的音频向量引擎")
        async with self._pool.lease([asset.snapshot()]) as inferencers:
            inferencer = cast(AudioEmbeddingInferencer, inferencers[(asset.id, asset.revision)])
            if self._embedding_limiter is None:
                return await inferencer.embed(pcm, algorithm=algorithm, expected_dimension=dimension, normalize=normalize)
            async with self._embedding_limiter:
                return await inferencer.embed(pcm, algorithm=algorithm, expected_dimension=dimension, normalize=normalize)

    async def preview_vad_audio(
        self,
        collection_name: str,
        audio: bytes,
        *,
        vad_engine_id: str,
        postprocessing: VadInferencer.Postprocessing | None = None,
    ) -> tuple[bytes, int]:
        space = await self.get_space(collection_name)
        if space.template != "voiceprint":
            raise ValueError("VAD 预览仅支持 voiceprint Collection")
        asset = await self._catalog.get_asset(vad_engine_id)
        if (
            asset.kind is not EngineKind.VAD
            or asset.api is not EngineApi.UX_VAD
            or not asset.enabled
        ):
            raise ValueError("所选引擎不是已启用的 VAD 引擎")
        pcm = await decode_audio(audio)

        async def detect() -> bytes:
            async with self._pool.lease([asset.snapshot()]) as inferencers:
                inferencer = cast(
                    VadInferencer,
                    inferencers[(asset.id, asset.revision)],
                )
                collector = VadBoundaryCollector(
                    len(pcm) // SAMPLE_WIDTH_BYTES
                )
                async for update in inferencer.stream_detect(
                    pcm_chunks(pcm),
                    SAMPLE_RATE,
                postprocessing=postprocessing,
                ):
                    if not isinstance(update, VadInferencer.Update):
                        raise TypeError(f"非法 VAD 更新类型: {type(update)!r}")
                    collector.accept(update)
                return concatenate_intervals(pcm, collector.finish())

        if self._embedding_limiter is None:
            trimmed = await detect()
        else:
            async with self._embedding_limiter:
                trimmed = await detect()
        if not trimmed:
            raise ValueError("VAD 未检测到有效语音")
        duration_ms = max(
            1,
            round(len(trimmed) / SAMPLE_WIDTH_BYTES * 1000 / SAMPLE_RATE),
        )
        return encode_wav(trimmed), duration_ms

    async def register_audio(
        self,
        collection_name: str,
        audio: bytes,
        configs: list[dict[str, Any]],
        payload: dict[str, Any],
        point_id: str | None,
        *,
        speaker_id: str | None = None,
        channel_id: str | None = None,
        captured_at: int | None = None,
        quality_score: float | None = None,
    ) -> str:
        space = await self.get_space(collection_name); definitions = {item.name: item for item in space.vectors}
        metadata = {
            key: value
            for key, value in {
                "speaker_id": speaker_id,
                "channel_id": channel_id,
                "captured_at": captured_at,
                "quality_score": quality_score,
            }.items()
            if value is not None
        }
        if space.template == "voiceprint":
            conflicts = sorted(VOICEPRINT_PAYLOAD_SCHEMA.keys() & payload.keys())
            if conflicts:
                raise ValueError(f"扩展 payload 不能包含保留字段: {', '.join(conflicts)}")
            payload = _validate_voiceprint_payload({
                **payload,
                **metadata,
                "captured_at": captured_at if captured_at is not None else time.time_ns() // 1_000_000,
                "quality_score": quality_score if quality_score is not None else 0.0,
            })
        else:
            payload = {**payload, **metadata}
        value = point_id or str(uuid.uuid4())
        if await self.gateway.point_exists(space.collection_name, value): raise VectorConflictError("Point ID 已存在")
        vectors: dict[str, tuple[float, ...]] = {}
        for config in configs:
            target = definitions.get(config["vector_name"])
            if target is None: raise ValueError(f"Named Vector 不存在: {config['vector_name']}")
            vectors[target.name] = await self.embed_audio(audio, engine_id=config["engine_id"], algorithm=config["algorithm"], dimension=target.size, normalize=config.get("normalize", True))
        if not vectors: raise ValueError("至少选择一个 Named Vector")
        if self._qdrant_limiter is None:
            await self.gateway.upsert(space.collection_name, value, vectors, payload)
        else:
            async with self._qdrant_limiter:
                await self.gateway.upsert(space.collection_name, value, vectors, payload)
        return value

    async def replace_payload(self, collection_name: str, point_id: str, payload: dict[str, Any]) -> None:
        space = await self.get_space(collection_name)
        if space.template == "voiceprint":
            payload = _validate_voiceprint_payload(payload)
        await self.gateway.replace_payload(collection_name, point_id, payload)

    async def search_embedding(self, collection_name: str, vector: tuple[float, ...], *, vector_name: str, top_k: int, score_threshold: float | None, filter_json: dict[str, Any] | None) -> tuple[SpeakerMatch, ...]:
        return await self._search_embedding(
            await self.get_space(collection_name), vector, vector_name=vector_name,
            top_k=top_k, score_threshold=score_threshold, filter_json=filter_json,
        )

    async def _search_embedding(self, space: VectorSpaceData, vector: tuple[float, ...], *, vector_name: str, top_k: int, score_threshold: float | None, filter_json: dict[str, Any] | None) -> tuple[SpeakerMatch, ...]:
        target = next((item for item in space.vectors if item.name == vector_name), None)
        if space.template != "voiceprint": raise ValueError("音频人物搜索仅支持 voiceprint Collection")
        if target is None: raise ValueError("Named Vector 不存在")
        if len(vector) != target.size: raise ValueError(f"查询向量维度不匹配: expected={target.size}, actual={len(vector)}")
        if self._qdrant_limiter is None:
            return await self.gateway.search_speakers(space.collection_name, vector_name, vector, limit=top_k, score_threshold=score_threshold, filter_json=filter_json)
        else:
            async with self._qdrant_limiter:
                return await self.gateway.search_speakers(space.collection_name, vector_name, vector, limit=top_k, score_threshold=score_threshold, filter_json=filter_json)

    async def search_audio(self, collection_name: str, audio: bytes, *, vector_name: str, engine_id: str, algorithm: str, normalize: bool, top_k: int, score_threshold: float | None, filter_json: dict[str, Any] | None) -> tuple[tuple[float, ...], tuple[SpeakerMatch, ...]]:
        space = await self.get_space(collection_name); target = next((item for item in space.vectors if item.name == vector_name), None)
        if space.template != "voiceprint": raise ValueError("音频人物搜索仅支持 voiceprint Collection")
        if target is None: raise ValueError("Named Vector 不存在")
        vector = await self.embed_audio(audio, engine_id=engine_id, algorithm=algorithm, dimension=target.size, normalize=normalize)
        return vector, await self._search_embedding(space, vector, vector_name=vector_name, top_k=top_k, score_threshold=score_threshold, filter_json=filter_json)
