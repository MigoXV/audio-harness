from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from audio_harness.configs.base import StrictModel
from audio_harness.configs.serving import HttpConfig, LimitsConfig
from audio_harness.configs.webrtc import WebRtcConfig
from audio_harness.configs.workflow import WorkflowRuntimeConfig


class VoiceConfig(StrictModel):
    storage_dir: Path = Path("./data-bin/voices")
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)


class ReceivedAudioCacheStorageConfig(StrictModel):
    storage_dir: Path = Path("./data-bin/cache")


class DatabaseConfig(StrictModel):
    url: str = Field(
        default="sqlite+aiosqlite:///./data-bin/sqlite/audio-harness.db",
        min_length=1,
    )

    @field_validator("url")
    @classmethod
    def require_async_sqlite(cls, value: str) -> str:
        if not value.startswith("sqlite+aiosqlite://"):
            raise ValueError("第一版数据库必须使用 sqlite+aiosqlite")
        return value


class QdrantConfig(StrictModel):
    grpc_target: str | None = None
    http_url: str | None = None

    @model_validator(mode="after")
    def require_pair(self) -> "QdrantConfig":
        if bool(self.grpc_target) != bool(self.http_url):
            raise ValueError("QDRANT_GRPC_TARGET 与 QDRANT_HTTP_URL 必须同时配置")
        return self


class AppConfig(StrictModel):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    workflow: WorkflowRuntimeConfig = Field(default_factory=WorkflowRuntimeConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    voices: VoiceConfig = Field(default_factory=VoiceConfig)
    received_audio_cache: ReceivedAudioCacheStorageConfig = Field(
        default_factory=ReceivedAudioCacheStorageConfig
    )
    webrtc: WebRtcConfig = Field(default_factory=WebRtcConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)

    @model_validator(mode="after")
    def validate_message_limits(self) -> "AppConfig":
        if self.workflow.min_chunk_bytes > self.limits.max_message_bytes:
            raise ValueError("min_chunk_bytes 不能大于 max_message_bytes")
        return self

    @classmethod
    def from_environment(cls) -> "AppConfig":
        defaults = cls()
        return cls.model_validate(
            {
                "database": {
                    "url": os.getenv("DATABASE_URL", defaults.database.url),
                },
                "voices": {
                    "storage_dir": os.getenv(
                        "VOICE_STORAGE_DIR",
                        str(defaults.voices.storage_dir),
                    ),
                },
                "received_audio_cache": {
                    "storage_dir": os.getenv(
                        "RECEIVED_AUDIO_CACHE_DIR",
                        str(defaults.received_audio_cache.storage_dir),
                    ),
                },
                "qdrant": {
                    "grpc_target": os.getenv("QDRANT_GRPC_TARGET"),
                    "http_url": os.getenv("QDRANT_HTTP_URL"),
                },
            }
        )
