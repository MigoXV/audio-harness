from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class AgentBackendKind(str, Enum):
    DSH = "dsh"


class AgentBackendProtocol(str, Enum):
    DSH_WEB_V1 = "dsh_web_v1"


class AgentBackendStatus(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


class AgentBackendEventKind(str, Enum):
    SESSION_STATUS = "session.status"
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TEXT_DELTA = "output.text.delta"
    MESSAGE_COMPLETED = "output.message.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    QUESTION_REQUESTED = "question.requested"
    QUESTION_RESOLVED = "question.resolved"
    ERROR = "error"


@dataclass(frozen=True)
class AgentBackendSnapshot:
    id: str
    revision: int
    name: str
    kind: AgentBackendKind
    protocol: AgentBackendProtocol
    target: str
    connect_timeout_seconds: float
    request_timeout_seconds: float
    headers_env: dict[str, str] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentBackendPlugin:
    entry_id: str
    module_name: str
    enabled: bool
    phase: str | None


@dataclass(frozen=True)
class AgentBackendCapabilities:
    wire_version: str
    provider: str
    model: str
    cwd: str
    presets: tuple[str, ...]
    plugins: tuple[AgentBackendPlugin, ...]


@dataclass(frozen=True)
class AgentBackendEvent:
    kind: AgentBackendEventKind
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class AgentBackendAssetData:
    id: str
    slug: str
    name: str
    kind: AgentBackendKind
    protocol: AgentBackendProtocol
    target: str
    connect_timeout_seconds: float
    request_timeout_seconds: float
    headers_env: dict[str, str]
    enabled: bool
    status: AgentBackendStatus
    description: str | None
    status_basis: str | None
    status_observed_at: datetime | None
    last_success_at: datetime | None
    probe_latency_ms: float | None
    consecutive_failures: int
    next_probe_at: datetime | None
    details: dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def snapshot(self) -> AgentBackendSnapshot:
        return AgentBackendSnapshot(
            id=self.id,
            revision=self.revision,
            name=self.name,
            kind=self.kind,
            protocol=self.protocol,
            target=self.target,
            connect_timeout_seconds=self.connect_timeout_seconds,
            request_timeout_seconds=self.request_timeout_seconds,
            headers_env=dict(self.headers_env),
            details=dict(self.details),
        )
