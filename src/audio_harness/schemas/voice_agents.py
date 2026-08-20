from __future__ import annotations

from datetime import datetime

from pydantic import Field

from audio_harness.schemas.base import StrictSchema
from audio_harness.voice_agents.models import VoiceAgentDefinition


class VoiceAgentCreate(StrictSchema):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    definition: VoiceAgentDefinition


class VoiceAgentRead(StrictSchema):
    id: str
    slug: str
    name: str
    description: str | None
    draft_revision: int
    active_revision: int | None
    created_at: datetime
    updated_at: datetime


class VoiceAgentPatch(StrictSchema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class VoiceAgentDraftUpdate(StrictSchema):
    expected_revision: int = Field(ge=1)
    definition: VoiceAgentDefinition


class VoiceAgentDraftRead(StrictSchema):
    id: str
    slug: str
    name: str
    description: str | None
    definition: VoiceAgentDefinition
    revision: int
    active_revision: int | None
    updated_at: datetime


class VoiceAgentRevisionRead(StrictSchema):
    id: str
    revision: int
    definition: VoiceAgentDefinition
    audio_workflow_id: str
    audio_workflow_revision: int
    engine_revisions: dict[str, int]
    agent_backend_revision: int | None = None


class ConfigurationDiagnosticRead(StrictSchema):
    code: str
    message: str
    field: str | None = None


class VoiceAgentValidationRead(StrictSchema):
    valid: bool
    diagnostics: list[ConfigurationDiagnosticRead]
