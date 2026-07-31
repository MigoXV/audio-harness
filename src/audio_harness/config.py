from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


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


class WorkflowConfig(StrictModel):
    min_chunk_bytes: int = Field(default=640, ge=2)
    min_segment_seconds: float = Field(default=0.5, ge=0)
    max_segment_seconds: float = Field(default=60.0, gt=0)
    audio_buffer_seconds: float = Field(default=120.0, gt=0)
    speech_pre_roll_seconds: float = Field(default=0.3, ge=0)
    default_language_code: str = Field(default="zh-CN", min_length=1)
    audio_queue_capacity: int = Field(default=32, gt=0)
    segment_queue_capacity: int = Field(default=8, gt=0)
    result_queue_capacity: int = Field(default=32, gt=0)

    @model_validator(mode="after")
    def validate_pcm_alignment(self) -> "WorkflowConfig":
        if self.min_chunk_bytes % 2:
            raise ValueError("min_chunk_bytes 必须为偶数，以保持 PCM S16LE 对齐")
        required_buffer = (
            self.max_segment_seconds + self.speech_pre_roll_seconds
        )
        if self.audio_buffer_seconds < required_buffer:
            raise ValueError(
                "audio_buffer_seconds 不能小于 "
                "max_segment_seconds + speech_pre_roll_seconds"
            )
        return self


class WorkflowRuntimeConfig(StrictModel):
    min_chunk_bytes: int = Field(default=640, ge=2)
    default_language_code: str = Field(default="zh-CN", min_length=1)
    audio_queue_capacity: int = Field(default=32, gt=0)
    segment_queue_capacity: int = Field(default=8, gt=0)
    result_queue_capacity: int = Field(default=32, gt=0)

    @field_validator("min_chunk_bytes")
    @classmethod
    def validate_pcm_alignment(cls, value: int) -> int:
        if value % 2:
            raise ValueError(
                "min_chunk_bytes 必须为偶数，以保持 PCM S16LE 对齐"
            )
        return value


class GrpcConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=50051, ge=1, le=65535)
    shutdown_grace_seconds: float = Field(default=5.0, ge=0)


class HttpConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    shutdown_grace_seconds: float = Field(default=5.0, ge=0)
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None


class RealtimeConfig(StrictModel):
    api_key: SecretStr | None = None


class IceServerConfig(StrictModel):
    urls: str | list[str]
    username: str | None = None
    credential: SecretStr | None = None

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, value: str | list[str]) -> str | list[str]:
        urls = [value] if isinstance(value, str) else value
        if not urls or any(
            not url.strip()
            or not url.startswith(("stun:", "turn:", "turns:"))
            for url in urls
        ):
            raise ValueError("ICE URL 必须使用 stun、turn 或 turns 协议")
        return value


class WebRtcConfig(StrictModel):
    ice_servers: list[IceServerConfig] = Field(default_factory=list)
    negotiation_timeout_seconds: float = Field(default=10.0, gt=0)
    disconnected_grace_seconds: float = Field(default=10.0, ge=0)


def parse_ice_servers_json(value: str) -> list[IceServerConfig]:
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("WebRTC ICE servers 必须是合法 JSON") from exc
    if not isinstance(raw, list):
        raise ValueError("WebRTC ICE servers JSON 顶层必须是数组")
    return [IceServerConfig.model_validate(item) for item in raw]


class LimitsConfig(StrictModel):
    max_message_bytes: int = Field(default=4 * 1024 * 1024, gt=0)


class ProtocolCapacityConfig(StrictModel):
    max_sessions: int = Field(default=64, gt=0)


class CapacityConfig(StrictModel):
    grpc: ProtocolCapacityConfig = Field(
        default_factory=ProtocolCapacityConfig
    )
    realtime: ProtocolCapacityConfig = Field(
        default_factory=ProtocolCapacityConfig
    )
    webrtc: ProtocolCapacityConfig = Field(
        default_factory=ProtocolCapacityConfig
    )
    max_asr_concurrency: int = Field(default=8, gt=0)


class DatabaseConfig(StrictModel):
    url: str = Field(
        default="sqlite+aiosqlite:///./data/audio-harness.db",
        min_length=1,
    )

    @field_validator("url")
    @classmethod
    def require_async_sqlite(cls, value: str) -> str:
        if not value.startswith("sqlite+aiosqlite://"):
            raise ValueError("第一版数据库必须使用 sqlite+aiosqlite")
        return value


class AppConfig(StrictModel):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    workflow: WorkflowRuntimeConfig = Field(
        default_factory=WorkflowRuntimeConfig
    )
    grpc: GrpcConfig = Field(default_factory=GrpcConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    realtime: RealtimeConfig = Field(default_factory=RealtimeConfig)
    webrtc: WebRtcConfig = Field(default_factory=WebRtcConfig)

    @model_validator(mode="after")
    def validate_message_limits(self) -> "AppConfig":
        if self.workflow.min_chunk_bytes > self.limits.max_message_bytes:
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
