from __future__ import annotations

from enum import Enum


class WorkflowKind(str, Enum):
    TRANSCRIPTION = "transcription"
    VOICE_AGENT = "voice_agent"


class ModelRouteVisibility(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"


DEFAULT_TRANSCRIPTION_WORKFLOW_ID = "default-transcription"
DEFAULT_TRANSCRIPTION_MODEL = "gpt-4o-transcribe"
GRPC_DEFAULT_MODEL = "grpc-default"
