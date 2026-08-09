from __future__ import annotations

from enum import Enum


class ModelRouteVisibility(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"


class RealtimeRouteTargetType(str, Enum):
    AUDIO_WORKFLOW = "audio_workflow"
    VOICE_AGENT = "voice_agent"


DEFAULT_AUDIO_WORKFLOW_ID = "default-audio"
DEFAULT_TRANSCRIPTION_MODEL = "gpt-4o-transcribe"
