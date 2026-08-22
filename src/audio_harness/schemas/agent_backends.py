from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from audio_harness.agent_backends.models import (
    AgentBackendKind,
    AgentBackendProtocol,
    AgentBackendStatus,
)
from audio_harness.schemas.base import StrictSchema


SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
ENV_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


class AgentBackendCreate(StrictSchema):
    slug: str = Field(min_length=1, max_length=100, pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    kind: AgentBackendKind = AgentBackendKind.DSH
    protocol: AgentBackendProtocol = AgentBackendProtocol.DSH_WEB_V1
    target: str = Field(min_length=1, max_length=2048)
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    headers_env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    description: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("slug", "name", "target")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value or "${" in value:
            raise ValueError("字段为空或包含未展开的环境变量")
        return value

    @field_validator("headers_env")
    @classmethod
    def validate_headers_env(cls, value: dict[str, str]) -> dict[str, str]:
        forbidden = {"connection", "content-length", "host", "upgrade"}
        result: dict[str, str] = {}
        for header, env_name in value.items():
            normalized = header.strip()
            if not normalized or any(ch in normalized for ch in "\r\n:"):
                raise ValueError("headers_env 包含非法请求头名称")
            if normalized.lower() in forbidden:
                raise ValueError(f"headers_env 不能覆盖 {normalized}")
            import re

            if re.fullmatch(ENV_PATTERN, env_name) is None:
                raise ValueError(f"环境变量名称不合法: {env_name}")
            result[normalized] = env_name
        return result


class AgentBackendPatch(StrictSchema):
    slug: str | None = Field(default=None, min_length=1, max_length=100, pattern=SLUG_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: AgentBackendKind | None = None
    protocol: AgentBackendProtocol | None = None
    target: str | None = Field(default=None, min_length=1, max_length=2048)
    connect_timeout_seconds: float | None = Field(default=None, gt=0, le=120)
    request_timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    headers_env: dict[str, str] | None = None
    enabled: bool | None = None
    description: str | None = None
    details: dict[str, Any] | None = None

    @field_validator("headers_env")
    @classmethod
    def validate_headers_env(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return value
        return AgentBackendCreate.validate_headers_env(value)


class AgentBackendRead(StrictSchema):
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
    details: dict[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime


class AgentBackendList(StrictSchema):
    items: list[AgentBackendRead]
    total: int
    limit: int
    offset: int


class AgentBackendPluginRead(StrictSchema):
    entry_id: str
    module_name: str
    enabled: bool
    phase: str | None


class AgentBackendCapabilitiesRead(StrictSchema):
    backend_id: str
    backend_revision: int
    wire_version: str
    provider: str
    model: str
    cwd: str
    presets: list[str]
    plugins: list[AgentBackendPluginRead]


class AgentBackendValidationRead(StrictSchema):
    backend_id: str
    name: str
    revision: int
    outcome: str
    message: str | None = None
    latency_ms: float | None = None


class AgentBackendUsageRead(StrictSchema):
    backend_id: str
    references: list[str] = Field(default_factory=list)
