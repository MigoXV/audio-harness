from __future__ import annotations

import asyncio
from typing import Any

import grpc
import httpx

from audio_harness.protos.qdrant import (
    collections_pb2,
    collections_service_pb2_grpc,
    json_with_int_pb2,
    points_pb2,
    points_service_pb2_grpc,
    qdrant_common_pb2,
)
from audio_harness.vectors.models import CollectionSchema, NamedVector, VectorMatch


class QdrantUnavailableError(RuntimeError):
    pass


class QdrantCollectionNotFoundError(LookupError):
    pass


def _value(value: Any) -> json_with_int_pb2.Value:
    if value is None:
        return json_with_int_pb2.Value(null_value=0)
    if isinstance(value, bool):
        return json_with_int_pb2.Value(bool_value=value)
    if isinstance(value, int):
        return json_with_int_pb2.Value(integer_value=value)
    if isinstance(value, float):
        return json_with_int_pb2.Value(double_value=value)
    if isinstance(value, str):
        return json_with_int_pb2.Value(string_value=value)
    if isinstance(value, list):
        return json_with_int_pb2.Value(
            list_value=json_with_int_pb2.ListValue(values=[_value(item) for item in value])
        )
    if isinstance(value, dict):
        return json_with_int_pb2.Value(
            struct_value=json_with_int_pb2.Struct(
                fields={str(key): _value(item) for key, item in value.items()}
            )
        )
    raise ValueError(f"payload 包含不支持的值: {type(value).__name__}")


def _python(value: json_with_int_pb2.Value) -> Any:
    kind = value.WhichOneof("kind")
    if kind == "null_value" or kind is None:
        return None
    if kind == "struct_value":
        return {key: _python(item) for key, item in value.struct_value.fields.items()}
    if kind == "list_value":
        return [_python(item) for item in value.list_value.values]
    return getattr(value, kind)


def point_id(value: str | int) -> qdrant_common_pb2.PointId:
    if isinstance(value, int) or str(value).isdigit():
        return qdrant_common_pb2.PointId(num=int(value))
    return qdrant_common_pb2.PointId(uuid=str(value))


def point_id_text(value: qdrant_common_pb2.PointId) -> str:
    kind = value.WhichOneof("point_id_options")
    return str(getattr(value, kind)) if kind else ""


_DISTANCE = {
    "cosine": collections_pb2.Cosine,
    "euclid": collections_pb2.Euclid,
    "dot": collections_pb2.Dot,
    "manhattan": collections_pb2.Manhattan,
}
_DISTANCE_NAME = {value: key for key, value in _DISTANCE.items()}
_PAYLOAD_SCHEMA_NAME = {
    collections_pb2.Keyword: "keyword",
    collections_pb2.Integer: "integer",
    collections_pb2.Float: "float",
    collections_pb2.Datetime: "datetime",
    collections_pb2.Bool: "bool",
    collections_pb2.Text: "text",
    collections_pb2.Uuid: "uuid",
}
_PAYLOAD_FIELD_TYPE = {
    "keyword": points_pb2.FieldTypeKeyword,
    "integer": points_pb2.FieldTypeInteger,
    "float": points_pb2.FieldTypeFloat,
    "datetime": points_pb2.FieldTypeDatetime,
    "bool": points_pb2.FieldTypeBool,
    "text": points_pb2.FieldTypeText,
    "uuid": points_pb2.FieldTypeUuid,
}


def _condition(raw: dict[str, Any]) -> qdrant_common_pb2.Condition:
    if "key" in raw:
        field = qdrant_common_pb2.FieldCondition(key=str(raw["key"]))
        if "match" in raw:
            match = raw["match"]
            item = match.get("value") if isinstance(match, dict) else match
            if isinstance(item, bool): field.match.boolean = item
            elif isinstance(item, int): field.match.integer = item
            elif isinstance(item, str): field.match.keyword = item
            else: raise ValueError("Filter match.value 仅支持字符串、整数或布尔值")
        elif "range" in raw:
            for key in ("lt", "lte", "gt", "gte"):
                if key in raw["range"]: setattr(field.range, key, float(raw["range"][key]))
        else:
            raise ValueError("字段条件必须包含 match 或 range")
        return qdrant_common_pb2.Condition(field=field)
    if "has_id" in raw:
        return qdrant_common_pb2.Condition(
            has_id=qdrant_common_pb2.HasIdCondition(
                has_id=[point_id(item) for item in raw["has_id"]]
            )
        )
    if "is_empty" in raw:
        return qdrant_common_pb2.Condition(
            is_empty=qdrant_common_pb2.IsEmptyCondition(key=str(raw["is_empty"]["key"]))
        )
    if "is_null" in raw:
        return qdrant_common_pb2.Condition(
            is_null=qdrant_common_pb2.IsNullCondition(key=str(raw["is_null"]["key"]))
        )
    if any(key in raw for key in ("must", "should", "must_not")):
        return qdrant_common_pb2.Condition(filter=filter_message(raw))
    raise ValueError("不支持的 Qdrant Filter 条件")


def filter_message(raw: dict[str, Any] | None) -> qdrant_common_pb2.Filter | None:
    if not raw:
        return None
    result = qdrant_common_pb2.Filter()
    for group in ("must", "should", "must_not"):
        for condition in raw.get(group, []):
            getattr(result, group).append(_condition(condition))
    return result


class QdrantGateway:
    def __init__(self, target: str | None, http_url: str | None = None) -> None:
        self.target = target
        self.http_url = http_url
        self._channel = grpc.aio.insecure_channel(target) if target else None
        self._collections = (
            collections_service_pb2_grpc.CollectionsStub(self._channel)
            if self._channel else None
        )
        self._points = points_service_pb2_grpc.PointsStub(self._channel) if self._channel else None

    def _require(self):
        if self._channel is None or self._collections is None or self._points is None:
            raise QdrantUnavailableError("Qdrant 尚未配置")

    async def ready(self) -> bool:
        if self._channel is None or self.http_url is None:
            return False
        try:
            await asyncio.wait_for(self._channel.channel_ready(), timeout=2)
            async with httpx.AsyncClient(timeout=2) as client:
                response = await client.get(f"{self.http_url.rstrip('/')}/readyz")
                response.raise_for_status()
            return True
        except (asyncio.TimeoutError, httpx.HTTPError):
            return False

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()

    async def create_collection(self, name: str, vectors: tuple[NamedVector, ...]) -> None:
        self._require()
        config = collections_pb2.VectorParamsMap(
            map={item.name: collections_pb2.VectorParams(size=item.size, distance=_DISTANCE[item.distance]) for item in vectors}
        )
        await self._collections.Create(
            collections_pb2.CreateCollection(
                collection_name=name,
                vectors_config=collections_pb2.VectorsConfig(params_map=config),
            )
        )

    async def list_collections(self) -> tuple[str, ...]:
        self._require()
        response = await self._collections.List(collections_pb2.ListCollectionsRequest())
        return tuple(sorted(item.name for item in response.collections))

    async def inspect_collection(self, name: str) -> CollectionSchema:
        self._require()
        try:
            response = await self._collections.Get(
                collections_pb2.GetCollectionInfoRequest(collection_name=name)
            )
        except grpc.aio.AioRpcError as exc:
            if exc.code() is grpc.StatusCode.NOT_FOUND:
                raise QdrantCollectionNotFoundError(f"Qdrant Collection 不存在: {name}") from exc
            raise
        config = response.result.config.params.vectors_config
        if config.WhichOneof("config") != "params_map":
            raise ValueError("v1 只支持 Named Vector Collection")
        vectors = tuple(sorted((NamedVector(key, int(value.size), _DISTANCE_NAME[value.distance]) for key, value in config.params_map.map.items()), key=lambda item: item.name))
        payload_schema = {
            key: _PAYLOAD_SCHEMA_NAME.get(value.data_type, "unknown")
            for key, value in response.result.payload_schema.items()
        }
        return CollectionSchema(vectors=vectors, payload_schema=payload_schema)

    async def create_payload_indexes(self, collection: str, schema: dict[str, str]) -> None:
        self._require()
        for field_name, field_type in schema.items():
            await self._points.CreateFieldIndex(
                points_pb2.CreateFieldIndexCollection(
                    collection_name=collection,
                    wait=True,
                    field_name=field_name,
                    field_type=_PAYLOAD_FIELD_TYPE[field_type],
                )
            )

    async def delete_collection(self, name: str) -> None:
        self._require()
        await self._collections.Delete(collections_pb2.DeleteCollection(collection_name=name))

    async def point_exists(self, collection: str, value: str | int) -> bool:
        self._require()
        response = await self._points.Get(points_pb2.GetPoints(collection_name=collection, ids=[point_id(value)]))
        return bool(response.result)

    async def upsert(self, collection: str, value: str | int, vectors: dict[str, tuple[float, ...]], payload: dict[str, Any]) -> None:
        self._require()
        named = points_pb2.NamedVectors(vectors={key: points_pb2.Vector(data=list(item)) for key, item in vectors.items()})
        await self._points.Upsert(points_pb2.UpsertPoints(
            collection_name=collection, wait=True,
            points=[points_pb2.PointStruct(id=point_id(value), payload={key: _value(item) for key, item in payload.items()}, vectors=points_pb2.Vectors(vectors=named))],
        ))

    async def search(self, collection: str, vector_name: str, vector: tuple[float, ...], *, limit: int, score_threshold: float | None = None, filter_json: dict[str, Any] | None = None) -> tuple[VectorMatch, ...]:
        self._require()
        request = points_pb2.SearchPoints(collection_name=collection, vector=list(vector), vector_name=vector_name, limit=limit, with_payload=points_pb2.WithPayloadSelector(enable=True))
        condition = filter_message(filter_json)
        if condition is not None: request.filter.CopyFrom(condition)
        if score_threshold is not None: request.score_threshold = score_threshold
        response = await self._points.Search(request)
        return tuple(VectorMatch(point_id_text(item.id), float(item.score), {key: _python(value) for key, value in item.payload.items()}) for item in response.result)

    async def scroll(self, collection: str, *, limit: int, offset: str | None, filter_json: dict[str, Any] | None, with_vectors: bool) -> tuple[list[dict[str, Any]], str | None]:
        self._require()
        request = points_pb2.ScrollPoints(collection_name=collection, limit=limit, with_payload=points_pb2.WithPayloadSelector(enable=True), with_vectors=points_pb2.WithVectorsSelector(enable=with_vectors))
        if offset: request.offset.CopyFrom(point_id(offset))
        condition = filter_message(filter_json)
        if condition is not None: request.filter.CopyFrom(condition)
        response = await self._points.Scroll(request)
        items = [{"point_id": point_id_text(item.id), "payload": {key: _python(value) for key, value in item.payload.items()}} for item in response.result]
        next_offset = point_id_text(response.next_page_offset) if response.HasField("next_page_offset") else None
        return items, next_offset

    async def replace_payload(self, collection: str, value: str | int, payload: dict[str, Any]) -> None:
        self._require()
        await self._points.OverwritePayload(points_pb2.SetPayloadPoints(collection_name=collection, wait=True, payload={key: _value(item) for key, item in payload.items()}, points_selector=points_pb2.PointsSelector(points=points_pb2.PointsIdsList(ids=[point_id(value)]))))

    async def delete_point(self, collection: str, value: str | int) -> None:
        self._require()
        await self._points.Delete(points_pb2.DeletePoints(collection_name=collection, wait=True, points=points_pb2.PointsSelector(points=points_pb2.PointsIdsList(ids=[point_id(value)]))))
