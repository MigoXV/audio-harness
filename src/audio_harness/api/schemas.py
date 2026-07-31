from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from audio_harness.assets import (
    EngineApi,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.config import CapacityConfig, WorkflowRuntimeConfig
from audio_harness.pipelines.dag import PipelineDefinition


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


class ErrorBody(StrictSchema):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(StrictSchema):
    error: ErrorBody


class HealthNodeRead(StrictSchema):
    node_id: str
    node_type: str
    state: str
    engine_id: str | None = None
    reason: str | None = None


class HealthResponse(StrictSchema):
    status: str
    database: str
    data_plane: str
    problems: list[str] = Field(default_factory=list)
    pipeline_revision: int | None = None
    nodes: list[HealthNodeRead] = Field(default_factory=list)


class WorkflowSettingsUpdate(WorkflowRuntimeConfig):
    revision: int = Field(ge=1)


class WorkflowSettingsRead(WorkflowRuntimeConfig):
    revision: int
    updated_at: datetime


class CapacitySettingsUpdate(CapacityConfig):
    revision: int = Field(ge=1)


class CapacitySettingsRead(CapacityConfig):
    revision: int
    updated_at: datetime


class PipelineDraftUpdate(StrictSchema):
    expected_revision: int = Field(ge=1)
    definition: PipelineDefinition


class PipelineActivateRequest(StrictSchema):
    expected_revision: int = Field(ge=1)


class PipelineDraftRead(StrictSchema):
    definition: PipelineDefinition
    revision: int
    updated_at: datetime
    active_revision: int | None


class PipelineRevisionRead(StrictSchema):
    definition: PipelineDefinition
    revision: int
    activated_at: datetime
    engine_revisions: dict[str, int]


class PipelineDiagnosticRead(StrictSchema):
    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    port: str | None = None


class PipelineValidationRead(StrictSchema):
    valid: bool
    diagnostics: list[PipelineDiagnosticRead]
    topological_order: list[str] = Field(default_factory=list)


class NodePortRead(StrictSchema):
    payload_type: str
    required: bool
    aggregate: bool


class NodeTypeRead(StrictSchema):
    type: str
    display_name: str
    input_ports: dict[str, NodePortRead]
    output_ports: dict[str, NodePortRead]
    config_schema: dict[str, Any]
    allowed_failure_policies: list[str]
    default_failure_policy: str
    required_api: str | None
    max_instances: int | None
    source: bool
    sink: bool
    version: int
