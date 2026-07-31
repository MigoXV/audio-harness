from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EngineKind(str, Enum):
    ASR = "asr"
    VAD = "vad"


class EngineApi(str, Enum):
    UX_SPEECH = "ux_speech"
    UX_VAD = "ux_vad"


class EngineProtocol(str, Enum):
    GRPC = "grpc"


class EngineStatus(str, Enum):
    OBSERVED_RUNNING = "observed_running"
    CONFIRMED_RUNNING = "confirmed_running"
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
        )
