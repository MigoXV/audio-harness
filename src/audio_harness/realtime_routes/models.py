from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ModelRouteVisibility(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"


class RealtimeRouteTargetType(str, Enum):
    AUDIO_WORKFLOW = "audio_workflow"
    VOICE_AGENT = "voice_agent"


@dataclass(frozen=True)
class RealtimeRouteSnapshot:
    model: str
    target_type: RealtimeRouteTargetType
    target_id: str
    visibility: ModelRouteVisibility
    updated_at: datetime

