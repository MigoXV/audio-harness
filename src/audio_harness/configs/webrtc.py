from __future__ import annotations

import json

from pydantic import Field, SecretStr, field_validator

from audio_harness.configs.base import StrictModel


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
