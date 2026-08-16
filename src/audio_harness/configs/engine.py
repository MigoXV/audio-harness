from __future__ import annotations

from pydantic import Field, field_validator

from audio_harness.configs.base import StrictModel


class EngineEndpoint(StrictModel):
    target: str = Field(min_length=1)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    request_timeout_seconds: float | None = Field(default=None, gt=0)

    @field_validator("target")
    @classmethod
    def reject_unexpanded_environment(cls, value: str) -> str:
        if "${" in value:
            raise ValueError(f"引擎地址包含未展开的环境变量: {value}")
        return value
