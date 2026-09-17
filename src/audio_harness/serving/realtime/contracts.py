"""Transport-independent Realtime contracts, with binary audio payloads.

Derive only the models containing audio from the existing protocol contracts.
Subclassing preserves the SDK's event dispatch types without duplicating its
nested schemas. JSON/Base64 and protobuf are codecs around these contracts.
"""

from __future__ import annotations

import copy
from functools import cache
from typing import Annotated, Any, Literal, Union, get_args, get_origin
import types

from pydantic import BaseModel, StrictBytes, TypeAdapter, create_model

from .protocol import AhaRealtimeClientEvent, AhaRealtimeServerEvent


def is_audio_field(model: type[BaseModel], name: str) -> bool:
    field = model.model_fields[name]
    if name == "audio":
        return str in (get_args(field.annotation) or (field.annotation,))
    if name == "delta":
        kind = model.model_fields.get("type")
        return kind is not None and "response.output_audio.delta" in get_args(
            kind.annotation
        )
    return False


@cache
def binary_type(annotation: Any) -> Any:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        fields = {}
        for name, info in annotation.model_fields.items():
            if is_audio_field(annotation, name):
                updated = (
                    StrictBytes | None
                    if type(None) in get_args(info.annotation)
                    else StrictBytes
                )
            else:
                updated = binary_type(info.annotation)
            if updated != info.annotation:
                fields[name] = (updated, copy.copy(info))
        if not fields:
            return annotation
        return create_model(
            "Binary" + annotation.__name__,
            __base__=annotation,
            __module__=__name__,
            **fields,
        )
    origin = get_origin(annotation)
    args = get_args(annotation)
    if not args or origin is Literal:
        return annotation
    if origin is Annotated:
        return Annotated.__class_getitem__((binary_type(args[0]), *args[1:]))
    updated = tuple(binary_type(arg) for arg in args)
    if updated == args:
        return annotation
    if origin in (Union, types.UnionType):
        return Union[updated]
    return origin[updated[0] if len(updated) == 1 else updated]


CLIENT_EVENTS = TypeAdapter(binary_type(AhaRealtimeClientEvent))
SERVER_EVENTS = TypeAdapter(binary_type(AhaRealtimeServerEvent))


def event_payload(event: Any) -> dict[str, Any]:
    return (
        event.model_dump(mode="python", exclude_none=True)
        if isinstance(event, BaseModel)
        else event
    )
