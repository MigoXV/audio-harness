from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from audio_harness.assets import (
    EngineApi,
    EngineKind,
    EngineProtocol,
    EngineRole,
    EngineStatus,
)
from audio_harness.config import CapacityConfig, WorkflowConfig


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EngineCreate(StrictSchema):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    kind: EngineKind
    api: EngineApi
    protocol: EngineProtocol = EngineProtocol.GRPC
    target: str = Field(min_length=1, max_length=500)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    enabled: bool = True
    status: EngineStatus = EngineStatus.OBSERVED_RUNNING
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
        if not value:
            raise ValueError("字段不能为空")
        if "${" in value:
            raise ValueError("字段包含未展开的环境变量")
        return value


class EnginePatch(StrictSchema):
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: EngineKind | None = None
    api: EngineApi | None = None
    protocol: EngineProtocol | None = None
    target: str | None = Field(default=None, min_length=1, max_length=500)
    connect_timeout_seconds: float | None = Field(default=None, gt=0)
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    enabled: bool | None = None
    status: EngineStatus | None = None
    image: str | None = Field(default=None, max_length=500)
    description: str | None = None
    status_basis: str | None = None
    status_observed_at: datetime | None = None
    source: str | None = Field(default=None, max_length=500)
    details: dict[str, Any] | None = None

    @field_validator("slug", "name", "target")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        if "${" in value:
            raise ValueError("字段包含未展开的环境变量")
        return value


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
    enabled: bool
    status: EngineStatus
    image: str | None
    description: str | None
    status_basis: str | None
    status_observed_at: datetime | None
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


class BindingUpdate(StrictSchema):
    engine_id: str = Field(min_length=1)


class BindingRead(StrictSchema):
    role: EngineRole
    engine: EngineRead | None
    generation: int
    updated_at: datetime | None


class ErrorBody(StrictSchema):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(StrictSchema):
    error: ErrorBody


class HealthResponse(StrictSchema):
    status: str
    database: str
    data_plane: str
    problems: list[str] = Field(default_factory=list)


class WorkflowSettingsUpdate(WorkflowConfig):
    revision: int = Field(ge=1)


class WorkflowSettingsRead(WorkflowConfig):
    revision: int
    updated_at: datetime


class CapacitySettingsUpdate(CapacityConfig):
    revision: int = Field(ge=1)


class CapacitySettingsRead(CapacityConfig):
    revision: int
    updated_at: datetime
