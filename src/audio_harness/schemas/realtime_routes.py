from __future__ import annotations

from datetime import datetime

from pydantic import Field

from audio_harness.realtime_routes.models import (
    ModelRouteVisibility,
    RealtimeRouteTargetType,
)
from audio_harness.schemas.base import StrictSchema


class RealtimeRouteUpsert(StrictSchema):
    model: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    target_type: RealtimeRouteTargetType
    target_id: str = Field(min_length=1, max_length=100)
    visibility: ModelRouteVisibility = ModelRouteVisibility.PUBLIC
    replace_existing: bool = False


class RealtimeRouteRead(StrictSchema):
    model: str
    target_type: RealtimeRouteTargetType
    target_id: str
    visibility: ModelRouteVisibility
    updated_at: datetime
