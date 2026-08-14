from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from audio_harness.configs import CapacityConfig, WorkflowRuntimeConfig
from audio_harness.schemas.base import StrictSchema


class WorkflowSettingsUpdate(WorkflowRuntimeConfig):
    revision: int = Field(ge=1)


class WorkflowSettingsRead(WorkflowRuntimeConfig):
    grpc_audio_workflow_id: str | None
    revision: int
    updated_at: datetime


class GrpcWorkflowBindingUpdate(StrictSchema):
    workflow_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)


class CapacitySettingsUpdate(CapacityConfig):
    revision: int = Field(ge=1)


class CapacitySettingsRead(CapacityConfig):
    revision: int
    updated_at: datetime


class RuntimeIssueRead(StrictSchema):
    code: str
    severity: Literal["warning", "error"]
    scope: Literal["control_plane", "grpc", "realtime"]
    message: str
    resource_id: str | None = None


class GrpcOverviewRead(StrictSchema):
    state: Literal["ready", "needs_configuration", "unavailable"]
    workflow_id: str | None
    workflow_revision: int | None


class RealtimeOverviewRead(StrictSchema):
    state: Literal["ready", "needs_configuration", "unavailable"]
    public_routes: int
    internal_routes: int


class RuntimeOverviewRead(StrictSchema):
    status: Literal["ready", "needs_configuration", "unavailable"]
    control_plane: Literal["ready", "needs_configuration", "unavailable"]
    grpc: GrpcOverviewRead
    realtime: RealtimeOverviewRead
    issues: list[RuntimeIssueRead]


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


class HealthResponse(StrictSchema):
    status: str
    database: str
    data_plane: str
    problems: list[str] = Field(default_factory=list)
    workflow_id: str | None = None
    workflow_revision: int | None = None


class VoiceAssetRead(StrictSchema):
    id: str
    name: str
    ref_text: str | None
    sample_rate: int
    duration_seconds: float
    created_at: datetime
