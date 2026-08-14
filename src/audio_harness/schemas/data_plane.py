from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from audio_harness.schemas.base import StrictSchema


class WebRtcOffer(StrictSchema):
    sdp: str = Field(min_length=1)
    session: dict[str, Any] | None = None
    model: str = "gpt-4o-transcribe"


class WebRtcAnswer(StrictSchema):
    id: str
    object: Literal["realtime.call"] = "realtime.call"
    status: Literal["active"] = "active"
    sdp: str
