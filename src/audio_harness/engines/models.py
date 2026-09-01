from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EngineKind(str, Enum):
    DENOISE = "denoise"
    ASR = "asr"
    VAD = "vad"
    LLM = "llm"
    TTS = "tts"
    EMBEDDING = "embedding"
    DIARIZATION = "diarization"


class EngineApi(str, Enum):
    UX_DENOISE = "ux_denoise"
    UX_SPEECH = "ux_speech"
    UX_VAD = "ux_vad"
    RESPONSES = "responses"
    QWEN_TTS_DUPLEX = "qwen_tts_duplex"
    ENGINE_FEATURE = "engine_feature"
    UX_SPEAKER_DIARIZATION = "ux_speaker_diarization"
    DUMMY_DIARIZE = "dummy_diarize"


class EngineProtocol(str, Enum):
    GRPC = "grpc"
    HTTPS = "https"
    DUMMY = "dummy"


class EngineStatus(str, Enum):
    UNKNOWN = "unknown"
    CHECKING = "checking"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STALE = "stale"
    DISABLED = "disabled"
    OBSERVED_RUNNING = "observed_running"
    CONFIRMED_RUNNING = "confirmed_running"
    UNAVAILABLE = "unavailable"
    INACTIVE = "inactive"


@dataclass(frozen=True)
class EngineSnapshot:
    id: str
    revision: int
    name: str
    api: EngineApi
    protocol: EngineProtocol
    target: str
    connect_timeout_seconds: float
    request_timeout_seconds: float | None
    credential_env: str | None = None
    api_key: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineAssetData:
    id: str
    slug: str
    name: str
    kind: EngineKind
    api: EngineApi
    protocol: EngineProtocol
    target: str
    connect_timeout_seconds: float
    request_timeout_seconds: float | None
    enabled: bool
    status: EngineStatus
    image: str | None
    description: str | None
    status_basis: str | None
    status_observed_at: datetime | None
    source: str | None
    credential_env: str | None = None
    api_key: str | None = None
    last_success_at: datetime | None = None
    probe_latency_ms: float | None = None
    consecutive_failures: int = 0
    next_probe_at: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def snapshot(self) -> EngineSnapshot:
        return EngineSnapshot(
            id=self.id,
            revision=self.revision,
            name=self.name,
            api=self.api,
            protocol=self.protocol,
            target=self.target,
            connect_timeout_seconds=self.connect_timeout_seconds,
            request_timeout_seconds=self.request_timeout_seconds,
            credential_env=self.credential_env,
            api_key=self.api_key,
            details=dict(self.details),
        )
