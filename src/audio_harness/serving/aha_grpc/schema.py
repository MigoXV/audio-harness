"""Compile the current Realtime schemas into an explicit protobuf contract.

The field-number ledger is append-only. Generated protobuf and codec metadata
are checked in: serving never runs a compiler or depends on grpcio-tools.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from audio_harness.serving.realtime.protocol import (
    AHA_REALTIME_CLIENT_EVENT_ADAPTER,
    AHA_REALTIME_SERVER_EVENT_ADAPTER,
)

PROTO_DIR = Path(__file__).resolve().parents[2] / "protos/aha_grpc/v1"


def name(value: str) -> str:
    return "".join(
        part[:1].upper() + part[1:]
        for part in re.split(r"[^a-zA-Z0-9]+", value)
        if part
    )


class Compiler:
    def __init__(self, ledger: dict[str, dict[str, int]]) -> None:
        self.ledger = ledger
        self.messages: dict[str, list[str]] = {}
        self.specs: dict[str, Any] = {}
        self.events: dict[str, dict[str, str]] = {}
        self.defs: dict[str, Any] = {}

    def number(self, message: str, field: str) -> int:
        fields = self.ledger.setdefault(message, {})
        if field not in fields:
            next_number = max(fields.values(), default=0) + 1
            if 19000 <= next_number <= 19999:
                next_number = 20000
            fields[field] = next_number
        return fields[field]

    def scalar(self, kind: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "proto": {
                "integer": "int64",
                "number": "double",
                "string": "string",
                "boolean": "bool",
                "binary": "bytes",
            }[kind],
        }

    def compile_type(self, schema: dict[str, Any], hint: str) -> dict[str, Any]:
        if "$ref" in schema:
            ref = schema["$ref"].rsplit("/", 1)[-1]
            self.compile_model(ref)
            return {"kind": "message", "proto": name(ref)}
        variants = schema.get("anyOf") or schema.get("oneOf")
        if variants:
            # Keep nullable and heterogeneous unions explicit, including null.
            message = hint + "Value"
            if message not in self.specs:
                fields = []
                cases = []
                for index, variant in enumerate(variants):
                    field = (
                        "null"
                        if variant.get("type") == "null"
                        else f"value_{index + 1}"
                    )
                    spec = self.compile_type(variant, message + name(field))
                    cases.append({"field": field, "schema": variant, "spec": spec})
                    fields.append(
                        f"    {spec['proto']} {field} = {self.number(message, field)};"
                    )
                self.messages[message] = ["  oneof kind {", *fields, "  }"]
                self.specs[message] = {"kind": "union", "cases": cases}
            return {"kind": "message", "proto": message}
        kind = schema.get("type")
        if kind == "null":
            return {"kind": "null", "proto": "google.protobuf.NullValue"}
        if kind in ("string", "integer", "number", "boolean", "binary"):
            return self.scalar(kind)
        if kind == "array":
            message = hint + "List"
            spec = self.compile_type(schema.get("items", {}), message + "Item")
            self.messages[message] = [
                f"  repeated {spec['proto']} values = {self.number(message, 'values')};"
            ]
            self.specs[message] = {"kind": "list", "item": spec}
            return {"kind": "message", "proto": message}
        if kind == "object" or "additionalProperties" in schema:
            if schema.get("properties"):
                self.compile_object(hint, schema)
                return {"kind": "message", "proto": hint}
            message = hint + "Map"
            additional = schema.get("additionalProperties", {})
            spec = self.compile_type(
                additional if isinstance(additional, dict) else {}, message + "Item"
            )
            self.messages[message] = [
                f"  map<string, {spec['proto']}> values = {self.number(message, 'values')};"
            ]
            self.specs[message] = {"kind": "map", "item": spec}
            return {"kind": "message", "proto": message}
        return {"kind": "json", "proto": "JsonValue"}

    def compile_object(self, message: str, schema: dict[str, Any]) -> None:
        self.specs[message] = {"kind": "object", "fields": {}}
        props = schema.get("properties", {})
        event_types = props.get("type", {})
        audio_delta = event_types.get("const") == "response.output_audio.delta"
        lines = []
        for field, original in props.items():
            value = json.loads(json.dumps(original))
            if field == "audio" or (field == "delta" and audio_delta):
                for candidate in [value, *value.get("anyOf", [])]:
                    if candidate.get("type") == "string":
                        candidate["type"] = "binary"
            spec = self.compile_type(value, message + name(field))
            self.specs[message]["fields"][field] = spec
            lines.append(
                f"  optional {spec['proto']} {field} = {self.number(message, field)};"
            )
        if schema.get("additionalProperties") is True:
            field = "additional_properties"
            self.specs[message]["extra"] = field
            lines.append(
                f"  map<string, JsonValue> {field} = {self.number(message, field)};"
            )
        self.messages[message] = lines

    def compile_model(self, ref: str) -> None:
        if name(ref) not in self.specs:
            self.compile_object(name(ref), self.defs[ref])

    def compile(self) -> tuple[str, dict[str, Any]]:
        schemas = {
            "ClientEvent": AHA_REALTIME_CLIENT_EVENT_ADAPTER.json_schema(),
            "ServerEvent": AHA_REALTIME_SERVER_EVENT_ADAPTER.json_schema(),
        }
        # Prefix definitions by direction to avoid SDK-local name collisions.
        for envelope, root in schemas.items():
            prefix = envelope.removesuffix("Event")

            def replace(value: Any) -> Any:
                if isinstance(value, dict):
                    return {
                        k: (
                            "#/$defs/" + prefix + v.rsplit("/", 1)[-1]
                            if k == "$ref"
                            else replace(v)
                        )
                        for k, v in value.items()
                    }
                if isinstance(value, list):
                    return [replace(item) for item in value]
                return value

            defs = {
                prefix + key: replace(value) for key, value in root["$defs"].items()
            }
            self.defs.update(defs)
            events = {}
            for case in replace(root["anyOf"]):
                ref = case["$ref"].rsplit("/", 1)[-1]
                kind = defs[ref]["properties"]["type"]
                for event_type in (
                    [kind["const"]] if "const" in kind else kind["enum"]
                ):
                    events.setdefault(
                        event_type, ref
                    )  # AHA Session overrides SDK Session.
            self.events[envelope] = {}
            lines = ["  oneof event {"]
            for event_type, ref in sorted(events.items()):
                self.compile_model(ref)
                field = event_type.replace(".", "_")
                self.events[envelope][event_type] = field
                lines.append(
                    f"    {name(ref)} {field} = {self.number(envelope, field)};"
                )
            lines.append("  }")
            self.messages[envelope] = lines
            self.specs[envelope] = {
                "kind": "events",
                "events": {
                    event: {"field": field, "proto": name(events[event])}
                    for event, field in self.events[envelope].items()
                },
            }
        header = """// Generated by scripts/generate_aha_grpc.py; field numbers are append-only.
syntax = "proto3";
package aha.grpc.v1;
import "google/protobuf/struct.proto";

service AhaRealtime {
  rpc Connect(stream ClientEvent) returns (stream ServerEvent);
}

// Open JSON leaves preserve integers without Struct's double conversion.
message JsonValue {
  oneof kind {
    google.protobuf.NullValue null = 1;
    bool boolean = 2;
    int64 integer = 3;
    double number = 4;
    string string = 5;
    JsonObject object = 6;
    JsonList list = 7;
  }
}
message JsonObject { map<string, JsonValue> values = 1; }
message JsonList { repeated JsonValue values = 1; }
"""
        chunks = [header]
        for message, fields in sorted(self.messages.items()):
            active = set(self.specs[message].get("fields", {}))
            spec = self.specs[message]
            if spec["kind"] == "events":
                active = {v["field"] for v in spec["events"].values()}
            elif spec["kind"] == "union":
                active = {v["field"] for v in spec["cases"]}
            elif spec["kind"] in ("map", "list"):
                active = {"values"}
            if spec.get("extra"):
                active.add(spec["extra"])
            retired = {
                key: n for key, n in self.ledger[message].items() if key not in active
            }
            reserved = (
                [
                    f"  reserved {', '.join(map(str, retired.values()))};",
                    f"  reserved {', '.join(json.dumps(k) for k in retired)};",
                ]
                if retired
                else []
            )
            chunks.append(
                "\nmessage "
                + message
                + " {\n"
                + "\n".join([*reserved, *fields])
                + "\n}\n"
            )
        return "".join(chunks), {"messages": self.specs, "definitions": self.defs}


def generate(*, check: bool = False) -> None:
    ledger_path = PROTO_DIR / "field_numbers.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    compiler = Compiler(ledger)
    proto, metadata = compiler.compile()
    outputs = {
        PROTO_DIR / "aha_realtime.proto": proto,
        PROTO_DIR
        / "codec_schema.json": json.dumps(
            metadata, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        ledger_path: json.dumps(ledger, indent=2, sort_keys=True) + "\n",
    }
    for path, content in outputs.items():
        if check:
            if not path.exists() or path.read_text() != content:
                raise RuntimeError(f"aha-grpc 协议需要重新生成: {path.name}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
