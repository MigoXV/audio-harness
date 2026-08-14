from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from audio_harness.engines.models import EngineApi
from audio_harness.schemas.base import StrictSchema
from audio_harness.workflows.models import AudioWorkflowDocument


class AudioWorkflowCreate(StrictSchema):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    document: AudioWorkflowDocument | None = None


class AudioWorkflowPatch(StrictSchema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class AudioWorkflowRead(StrictSchema):
    id: str
    slug: str
    name: str
    description: str | None
    draft_revision: int
    active_revision: int | None
    created_at: datetime
    updated_at: datetime


class AudioWorkflowDraftUpdate(StrictSchema):
    expected_revision: int = Field(ge=1)
    document: AudioWorkflowDocument


class AudioWorkflowDraftRead(StrictSchema):
    id: str
    slug: str
    name: str
    description: str | None
    document: AudioWorkflowDocument
    revision: int
    active_revision: int | None
    updated_at: datetime


class PublishRequest(StrictSchema):
    expected_revision: int = Field(ge=1)


class RestoreRequest(StrictSchema):
    expected_revision: int = Field(ge=1)


class AudioWorkflowRevisionRead(StrictSchema):
    workflow_id: str
    revision: int
    document: AudioWorkflowDocument
    engine_revisions: dict[str, int]
    activated_at: datetime | None = None


class GraphDiagnosticRead(StrictSchema):
    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    port: str | None = None


class AudioWorkflowValidationRead(StrictSchema):
    valid: bool
    diagnostics: list[GraphDiagnosticRead]
    topological_order: list[str] = Field(default_factory=list)


class NodePortRead(StrictSchema):
    name: str
    kind: str


class AudioNodeTypeRead(StrictSchema):
    kind: str
    display_name: str
    required_api: EngineApi
    inputs: list[NodePortRead]
    outputs: list[NodePortRead]
    config_schema: dict[str, Any]
