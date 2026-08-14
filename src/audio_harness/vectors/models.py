from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


VOICEPRINT_PAYLOAD_SCHEMA: dict[str, str] = {
    "speaker_id": "keyword",
    "channel_id": "keyword",
    "captured_at": "integer",
    "quality_score": "float",
    "cluster_id": "integer",
}


@dataclass(frozen=True)
class NamedVector:
    name: str
    size: int
    distance: str


@dataclass(frozen=True)
class CollectionSchema:
    vectors: tuple[NamedVector, ...]
    payload_schema: Mapping[str, str]


@dataclass(frozen=True)
class VectorSpaceData:
    collection_name: str
    vectors: tuple[NamedVector, ...]
    template: str | None = None


@dataclass(frozen=True)
class VectorMatch:
    point_id: str
    score: float
    payload: dict[str, object]
