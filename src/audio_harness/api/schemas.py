from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from audio_harness.assets import (
    EngineApi,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.config import CapacityConfig, WorkflowRuntimeConfig
from audio_harness.pipelines.dag import PipelineDefinition
from audio_harness.pipelines.stages import (
    StageWorkflowDefinition,
    WorkflowDefinition,
)
from audio_harness.workflow_types import ModelRouteVisibility, WorkflowKind


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
    credential_env: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
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
    credential_env: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    api_key: str | None = Field(default=None, min_length=1, max_length=8192)
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
    definition: WorkflowDefinition


class PipelineActivateRequest(StrictSchema):
    expected_revision: int = Field(ge=1)


class PipelineRestoreRequest(StrictSchema):
    expected_revision: int = Field(ge=1)


class PipelineDraftRead(StrictSchema):
    workflow_id: str
    definition: WorkflowDefinition
    revision: int
    updated_at: datetime
    active_revision: int | None


class WorkflowCreate(StrictSchema):
    slug: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    name: str = Field(min_length=1, max_length=200)
    kind: WorkflowKind
    description: str | None = None
    definition: StageWorkflowDefinition | PipelineDefinition | None = None


class WorkflowPatch(StrictSchema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class WorkflowRead(StrictSchema):
    id: str
    slug: str
    name: str
    kind: WorkflowKind
    description: str | None
    draft_revision: int
    active_revision: int | None
    created_at: datetime
    updated_at: datetime


class WorkflowStageStatusRead(StrictSchema):
    stage: str
    engine_id: str | None
    engine_name: str | None = None
    health: str
    reason: str | None = None
    checked_at: datetime | None = None


class WorkflowStatusRead(StrictSchema):
    workflow_id: str
    state: str
    draft_revision: int
    active_revision: int | None
    has_unpublished_changes: bool
    public_models: list[str] = Field(default_factory=list)
    diagnostics: list[PipelineDiagnosticRead] = Field(default_factory=list)
    stages: list[WorkflowStageStatusRead] = Field(default_factory=list)


class RuntimeLimiterRead(StrictSchema):
    active: int
    capacity: int
    rejected: int


class RuntimeStatusRead(StrictSchema):
    grpc: RuntimeLimiterRead
    realtime: RuntimeLimiterRead
    webrtc: RuntimeLimiterRead
    asr: RuntimeLimiterRead
    responses: RuntimeLimiterRead
    tts: RuntimeLimiterRead
    started_at: datetime


class TestSessionCreate(StrictSchema):
    workflow_id: str = Field(min_length=1, max_length=100)
    protocol: Literal["wss", "webrtc"]


class TestSessionRead(StrictSchema):
    id: str
    workflow_id: str
    model: str
    protocol: Literal["wss", "webrtc"]
    endpoint: str
    expires_at: datetime


class ModelRouteUpsert(StrictSchema):
    model: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    workflow_id: str = Field(min_length=1, max_length=100)
    visibility: ModelRouteVisibility = ModelRouteVisibility.PUBLIC


class ModelRouteRead(StrictSchema):
    model: str
    workflow_id: str
    visibility: ModelRouteVisibility
    updated_at: datetime


class VoiceAssetRead(StrictSchema):
    id: str
    name: str
    ref_text: str | None
    sample_rate: int
    duration_seconds: float
    created_at: datetime


class PipelineRevisionRead(StrictSchema):
    definition: WorkflowDefinition
    revision: int
    activated_at: datetime
    engine_revisions: dict[str, int]


class PipelineDiagnosticRead(StrictSchema):
    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    port: str | None = None
    stage: str | None = None
    field: str | None = None


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
