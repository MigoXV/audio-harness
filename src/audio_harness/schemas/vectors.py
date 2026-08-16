from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from audio_harness.schemas.base import StrictSchema


class NamedVectorSchema(StrictSchema):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    size: int = Field(gt=0, le=65536)
    distance: Literal["cosine", "euclid", "dot", "manhattan"] = "cosine"


class VectorSpaceCreate(StrictSchema):
    collection_name: str = Field(min_length=1, max_length=255)
    vectors: list[NamedVectorSchema] = Field(min_length=1)
    template: Literal["voiceprint"] | None = None

    @field_validator("vectors")
    @classmethod
    def unique_vectors(cls, value: list[NamedVectorSchema]) -> list[NamedVectorSchema]:
        if len({item.name for item in value}) != len(value):
            raise ValueError("Named Vector 名称不能重复")
        return value


class VectorSpaceRead(StrictSchema):
    collection_name: str
    vectors: list[NamedVectorSchema]
    template: Literal["voiceprint"] | None = None


class VectorSpaceList(StrictSchema):
    items: list[VectorSpaceRead]


class InfrastructureRead(StrictSchema):
    configured: bool
    ready: bool
    grpc_target: str | None
    http_url: str | None


class DestroyRequest(StrictSchema):
    confirmation: str


class ScrollRequest(StrictSchema):
    filter: dict[str, Any] | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: str | None = None
    with_vectors: bool = False


class PayloadReplace(StrictSchema):
    payload: dict[str, Any]


class VectorObservationRead(StrictSchema):
    point_id: str
    payload: dict[str, Any]


class SpeakerMatchRead(StrictSchema):
    speaker_id: str
    score: float
    observation: VectorObservationRead


class AudioSpeakerSearchRead(StrictSchema):
    embedding: list[float]
    groups: list[SpeakerMatchRead]


class VadAudioPreviewRead(StrictSchema):
    audio_base64: str
    content_type: Literal["audio/wav"] = "audio/wav"
    duration_ms: int = Field(gt=0)
