"""JSON/Base64 boundary used by WebSocket and WebRTC event channels."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from pydantic import BaseModel

from .contracts import CLIENT_EVENTS, SERVER_EVENTS, event_payload, is_audio_field
from .protocol import AHA_REALTIME_CLIENT_EVENT_ADAPTER


class InvalidAudioEncoding(ValueError):
    pass


def _decode_model(value: Any) -> Any:
    if isinstance(value, BaseModel):
        result = {}
        for name in value.model_fields_set:
            item = getattr(value, name)
            if (
                name in type(value).model_fields
                and is_audio_field(type(value), name)
                and item is not None
            ):
                try:
                    item = base64.b64decode(item, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise InvalidAudioEncoding("audio 必须是合法的 base64 PCM") from exc
            result[name] = _decode_model(item)
        return result
    if isinstance(value, list):
        return [_decode_model(item) for item in value]
    if isinstance(value, dict):
        return {key: _decode_model(item) for key, item in value.items()}
    return value


def decode_client_event(payload: dict[str, Any]) -> Any:
    wire = AHA_REALTIME_CLIENT_EVENT_ADAPTER.validate_python(payload)
    return CLIENT_EVENTS.validate_python(_decode_model(wire))


def _encode(value: Any) -> Any:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def encode_server_event(event: Any) -> dict[str, Any]:
    payload = event_payload(event)
    SERVER_EVENTS.validate_python(payload)
    return _encode(payload)
