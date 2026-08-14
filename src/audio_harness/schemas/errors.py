from typing import Any

from audio_harness.schemas.base import StrictSchema


class ErrorBody(StrictSchema):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(StrictSchema):
    error: ErrorBody
