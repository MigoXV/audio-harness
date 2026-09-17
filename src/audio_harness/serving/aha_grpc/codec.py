"""Lossless canonical-event ↔ strongly typed protobuf conversion.

No JSON serialization or Base64 occurs on this transport. The generated schema
metadata describes only wrappers, unions and field presence; business validation
belongs to the shared Realtime service.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

from google.protobuf.message import Message

from audio_harness.protos.aha_grpc.v1 import aha_realtime_pb2 as pb
from audio_harness.serving.realtime.contracts import (
    CLIENT_EVENTS,
    SERVER_EVENTS,
    event_payload,
)


@cache
def _schema() -> dict[str, Any]:
    return json.loads(
        files("audio_harness.protos.aha_grpc.v1")
        .joinpath("codec_schema.json")
        .read_text()
    )


def _matches(value: Any, schema: dict[str, Any]) -> bool:
    if "$ref" in schema:
        return _matches(
            value, _schema()["definitions"][schema["$ref"].rsplit("/", 1)[-1]]
        )
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if "anyOf" in schema:
        return any(_matches(value, candidate) for candidate in schema["anyOf"])
    kind = schema.get("type")
    if kind == "null":
        return value is None
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "string":
        return isinstance(value, (str, bytes))
    if kind == "binary":
        return isinstance(value, bytes)
    if kind == "array":
        return isinstance(value, list) and all(
            _matches(v, schema.get("items", {})) for v in value
        )
    if kind == "object":
        if not isinstance(value, dict):
            return False
        props = schema.get("properties", {})
        return all(k in value for k in schema.get("required", [])) and all(
            _matches(value[k], s) for k, s in props.items() if k in value
        )
    return True


def _encode_json(target: Message, value: Any) -> None:
    if value is None:
        target.null = 0
    elif isinstance(value, bool):
        target.boolean = value
    elif isinstance(value, int):
        target.integer = value
    elif isinstance(value, float):
        target.number = value
    elif isinstance(value, str):
        target.string = value
    elif isinstance(value, dict):
        target.object.SetInParent()
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _encode_json(target.object.values[key], item)
    elif isinstance(value, list):
        target.list.SetInParent()
        for item in value:
            _encode_json(target.list.values.add(), item)
    else:
        raise ValueError(f"Unsupported JSON leaf: {type(value).__name__}")


def _decode_json(value: Message) -> Any:
    kind = value.WhichOneof("kind")
    if kind is None:
        raise ValueError("JsonValue.kind is required")
    if kind == "null":
        return None
    if kind == "object":
        return {k: _decode_json(v) for k, v in value.object.values.items()}
    if kind == "list":
        return [_decode_json(v) for v in value.list.values]
    return getattr(value, kind)


def _set(target: Message, field: str, spec: dict[str, Any], value: Any) -> None:
    if spec["kind"] in ("message", "json"):
        child = getattr(target, field)
        child.SetInParent()
        if spec["kind"] == "json":
            _encode_json(child, value)
        else:
            _encode_message(child, value)
    else:
        setattr(target, field, 0 if spec["kind"] == "null" else value)


def _get(value: Any, spec: dict[str, Any]) -> Any:
    if spec["kind"] == "json":
        return _decode_json(value)
    if spec["kind"] == "message":
        return _decode_message(value)
    if spec["kind"] == "null":
        return None
    return value


def _encode_message(target: Message, value: Any) -> None:
    spec = _schema()["messages"][target.DESCRIPTOR.name]
    kind = spec["kind"]
    if kind == "object":
        for field, item in value.items():
            if field in spec["fields"]:
                _set(target, field, spec["fields"][field], item)
            elif spec.get("extra"):
                _encode_json(getattr(target, spec["extra"])[field], item)
            else:
                raise ValueError(f"Unknown field {target.DESCRIPTOR.name}.{field}")
    elif kind == "union":
        for case in spec["cases"]:
            if _matches(value, case["schema"]):
                _set(target, case["field"], case["spec"], value)
                return
        raise ValueError(f"No union variant matches {target.DESCRIPTOR.name}")
    elif kind == "list":
        item_spec = spec["item"]
        for item in value:
            if item_spec["kind"] in ("message", "json"):
                child = target.values.add()
                if item_spec["kind"] == "json":
                    _encode_json(child, item)
                else:
                    _encode_message(child, item)
            else:
                target.values.append(item)
    elif kind == "map":
        item_spec = spec["item"]
        for key, item in value.items():
            if item_spec["kind"] == "message":
                _encode_message(target.values[key], item)
            elif item_spec["kind"] == "json":
                _encode_json(target.values[key], item)
            else:
                target.values[key] = item
    elif kind == "events":
        event = spec["events"].get(value.get("type"))
        if event is None:
            raise ValueError("Unknown Realtime event type")
        _encode_message(getattr(target, event["field"]), value)


def _decode_message(value: Message) -> Any:
    spec = _schema()["messages"][value.DESCRIPTOR.name]
    kind = spec["kind"]
    if kind == "object":
        result = {
            field: _get(getattr(value, field), child)
            for field, child in spec["fields"].items()
            if value.HasField(field)
        }
        if spec.get("extra"):
            for key, item in getattr(value, spec["extra"]).items():
                if key in spec["fields"]:
                    raise ValueError(
                        "additional_properties cannot override declared fields"
                    )
                result[key] = _decode_json(item)
        return result
    if kind == "union":
        selected = value.WhichOneof("kind")
        if selected is None:
            raise ValueError(f"{value.DESCRIPTOR.name}.kind is required")
        case = next(case for case in spec["cases"] if case["field"] == selected)
        result = _get(getattr(value, selected), case["spec"])
        if not _matches(result, case["schema"]):
            raise ValueError("Invalid union variant value")
        return result
    if kind == "list":
        return [_get(item, spec["item"]) for item in value.values]
    if kind == "map":
        return {k: _get(v, spec["item"]) for k, v in value.values.items()}
    selected = value.WhichOneof("event")
    if selected is None:
        raise ValueError("Exactly one Realtime event is required")
    result = _decode_message(getattr(value, selected))
    expected = next(
        event for event, case in spec["events"].items() if case["field"] == selected
    )
    if result.get("type") != expected:
        raise ValueError("Event type does not match protobuf oneof branch")
    return result


def encode_client_event(event: Any) -> pb.ClientEvent:
    # Preserve unset versus explicit null/empty values, including nested fields.
    parsed = CLIENT_EVENTS.validate_python(event)
    value = parsed.model_dump(mode="python", exclude_unset=True)
    result = pb.ClientEvent()
    _encode_message(result, value)
    return result


def decode_client_event(event: pb.ClientEvent) -> dict[str, Any]:
    return _decode_message(event)


def encode_server_event(event: Any) -> pb.ServerEvent:
    value = event_payload(event)
    SERVER_EVENTS.validate_python(value)
    result = pb.ServerEvent()
    _encode_message(result, value)
    return result


def decode_server_event(event: pb.ServerEvent) -> dict[str, Any]:
    value = _decode_message(event)
    SERVER_EVENTS.validate_python(value)
    return value
