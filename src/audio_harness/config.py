from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EngineEndpoint(StrictModel):
    target: str = Field(min_length=1)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    request_timeout_seconds: Optional[float] = Field(default=None, gt=0)

    @field_validator("target")
    @classmethod
    def reject_unexpanded_environment(cls, value: str) -> str:
        if "${" in value:
            raise ValueError(f"引擎地址包含未展开的环境变量: {value}")
        return value


class EnginesConfig(StrictModel):
    asr: EngineEndpoint
    vad: EngineEndpoint
    semantic_vad: Optional[EngineEndpoint] = None


class WorkflowConfig(StrictModel):
    min_chunk_bytes: int = Field(default=6400, ge=2)
    min_segment_seconds: float = Field(default=0.5, ge=0)
    max_segment_seconds: float = Field(default=60.0, gt=0)
    default_language_code: str = Field(default="zh-CN", min_length=1)
    audio_queue_capacity: int = Field(default=32, gt=0)
    segment_queue_capacity: int = Field(default=8, gt=0)
    result_queue_capacity: int = Field(default=32, gt=0)
    max_sessions: int = Field(default=64, gt=0)
    max_asr_concurrency: int = Field(default=8, gt=0)

    @model_validator(mode="after")
    def validate_pcm_alignment(self) -> "WorkflowConfig":
        if self.min_chunk_bytes % 2:
            raise ValueError("min_chunk_bytes 必须为偶数，以保持 PCM S16LE 对齐")
        return self


class ServingConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=50051, ge=1, le=65535)
    max_message_bytes: int = Field(default=4 * 1024 * 1024, gt=0)
    shutdown_grace_seconds: float = Field(default=5.0, ge=0)


class AppConfig(StrictModel):
    engines: EnginesConfig
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    serving: ServingConfig = Field(default_factory=ServingConfig)

    @model_validator(mode="after")
    def validate_message_limits(self) -> "AppConfig":
        if self.workflow.min_chunk_bytes > self.serving.max_message_bytes:
            raise ValueError("min_chunk_bytes 不能大于 max_message_bytes")
        return self

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        config_path = Path(path)
        expanded = os.path.expandvars(config_path.read_text(encoding="utf-8"))
        raw: Any = yaml.safe_load(expanded)
        if not isinstance(raw, dict):
            raise ValueError("配置文件顶层必须是对象")
        return cls.model_validate(raw)
