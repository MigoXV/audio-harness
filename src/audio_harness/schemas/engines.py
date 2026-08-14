from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from audio_harness.engines.models import EngineApi, EngineKind, EngineProtocol, EngineStatus
from audio_harness.schemas.base import StrictSchema


class EngineCreate(StrictSchema):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    kind: EngineKind
    api: EngineApi
    protocol: EngineProtocol = EngineProtocol.GRPC
    target: str = Field(min_length=1, max_length=500)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    credential_env: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    api_key: str | None = Field(default=None, min_length=1, max_length=8192)
    enabled: bool = True
    status: EngineStatus = EngineStatus.UNKNOWN
    image: str | None = Field(default=None, max_length=500)
    description: str | None = None
    status_basis: str | None = None
    status_observed_at: datetime | None = None
    source: str | None = Field(default=None, max_length=500)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("slug", "name", "target")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value or "${" in value:
            raise ValueError("字段为空或包含未展开的环境变量")
        return value


class EnginePatch(StrictSchema):
    slug: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: EngineKind | None = None
    api: EngineApi | None = None
    protocol: EngineProtocol | None = None
    target: str | None = Field(default=None, min_length=1, max_length=500)
    connect_timeout_seconds: float | None = Field(default=None, gt=0)
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    credential_env: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=8192)
    enabled: bool | None = None
    status: EngineStatus | None = None
    image: str | None = Field(default=None, max_length=500)
    description: str | None = None
    status_basis: str | None = None
    status_observed_at: datetime | None = None
    source: str | None = Field(default=None, max_length=500)
    details: dict[str, Any] | None = None


class EngineRead(StrictSchema):
    id: str
    slug: str
    name: str
    kind: EngineKind
    api: EngineApi
    protocol: EngineProtocol
    target: str
    connect_timeout_seconds: float
    request_timeout_seconds: float | None
    credential_env: str | None
    api_key: str | None
    enabled: bool
    status: EngineStatus
    image: str | None
    description: str | None
    status_basis: str | None
    status_observed_at: datetime | None
    last_success_at: datetime | None
    probe_latency_ms: float | None
    consecutive_failures: int
    next_probe_at: datetime | None
    source: str | None
    details: dict[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime


class EngineList(StrictSchema):
    items: list[EngineRead]
    total: int
    limit: int
    offset: int


class EngineModelListRead(StrictSchema):
    engine_id: str
    engine_revision: int
    items: list[str]


class EngineValidationResultRead(StrictSchema):
    engine_id: str
    name: str
    revision: int
    outcome: str
    message: str | None = None
    latency_ms: float | None = None


class EngineValidationRead(StrictSchema):
    results: list[EngineValidationResultRead]


class EngineUsageRead(StrictSchema):
    engine_id: str
    references: list[str] = Field(default_factory=list)
