from __future__ import annotations

import base64
import binascii
import copy
import io
import wave
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class ConversationMutationError(ValueError):
    message: str
    code: str = "invalid_conversation_item"
    param: str | None = None

    def __str__(self) -> str:
        return self.message


class RealtimeConversation:
    """Bounded, ordered Realtime conversation with Responses projection."""

    def __init__(self, *, max_items: int) -> None:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self._max_items = max_items
        self._items: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    @property
    def last_item_id(self) -> str | None:
        return self._order[-1] if self._order else None

    def __len__(self) -> int:
        return len(self._order)

    def contains(self, item_id: str) -> bool:
        return item_id in self._items

    def create(
        self,
        item: Any,
        *,
        previous_item_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        payload = (
            item.model_dump(mode="json", exclude_none=True)
            if hasattr(item, "model_dump")
            else copy.deepcopy(item)
        )
        self._validate_item(payload)
        identifier = str(payload.get("id") or f"item_{uuid4().hex}")
        if identifier in self._items:
            raise ConversationMutationError(
                f"conversation item 已存在: {identifier}",
                code="item_already_exists",
                param="item.id",
            )
        if previous_item_id is not None and previous_item_id not in self._items:
            raise ConversationMutationError(
                f"previous_item_id 不存在: {previous_item_id}",
                code="item_not_found",
                param="previous_item_id",
            )
        payload["id"] = identifier
        payload.setdefault("object", "realtime.item")
        payload["status"] = "completed"
        actual_previous = (
            previous_item_id
            if previous_item_id is not None
            else self.last_item_id
        )
        if previous_item_id is None:
            self._order.append(identifier)
        else:
            index = self._order.index(previous_item_id) + 1
            self._order.insert(index, identifier)
        self._items[identifier] = payload
        self._trim()
        return copy.deepcopy(payload), actual_previous

    @staticmethod
    def _validate_item(item: dict[str, Any]) -> None:
        item_type = item.get("type")
        if item_type == "message":
            role = item.get("role")
            allowed = {
                "system": {"input_text"},
                "user": {"input_text", "input_image", "input_audio"},
                "assistant": {"output_text", "output_audio"},
            }
            if role not in allowed:
                raise ConversationMutationError(
                    f"不支持的 message role: {role}",
                    param="item.role",
                )
            content = item.get("content")
            if not isinstance(content, list) or not content:
                raise ConversationMutationError(
                    "message content 不能为空",
                    param="item.content",
                )
            for index, part in enumerate(content):
                part_type = part.get("type") if isinstance(part, dict) else None
                param = f"item.content[{index}]"
                if part_type not in allowed[role]:
                    raise ConversationMutationError(
                        f"{role} 消息不支持 content type: {part_type}",
                        param=f"{param}.type",
                    )
                if part_type in {"input_text", "output_text"} and not isinstance(
                    part.get("text"),
                    str,
                ):
                    raise ConversationMutationError(
                        f"{part_type} 必须包含 text",
                        param=f"{param}.text",
                    )
                if part_type == "input_image" and not isinstance(
                    part.get("image_url"),
                    str,
                ):
                    raise ConversationMutationError(
                        "input_image 必须包含 image_url",
                        param=f"{param}.image_url",
                    )
                if part_type in {"input_audio", "output_audio"} and not (
                    isinstance(part.get("audio"), str)
                    or isinstance(part.get("transcript"), str)
                ):
                    raise ConversationMutationError(
                        f"{part_type} 必须包含 audio 或 transcript",
                        param=param,
                    )
            return
        if item_type in {
            "function_call",
            "function_call_output",
            "mcp_approval_response",
            "mcp_list_tools",
            "mcp_call",
            "mcp_approval_request",
        }:
            return
        raise ConversationMutationError(
            f"不支持的 conversation item type: {item_type}",
            param="item.type",
        )

    def upsert_completed(
        self,
        item: dict[str, Any],
        *,
        previous_item_id: str | None = None,
    ) -> dict[str, Any]:
        payload = copy.deepcopy(item)
        payload["status"] = "completed"
        return self.upsert(payload, previous_item_id=previous_item_id)

    def upsert(
        self,
        item: dict[str, Any],
        *,
        previous_item_id: str | None = None,
    ) -> dict[str, Any]:
        identifier = str(item["id"])
        payload = copy.deepcopy(item)
        payload.setdefault("object", "realtime.item")
        payload.setdefault("status", "completed")
        if identifier not in self._items:
            if previous_item_id is not None and previous_item_id in self._items:
                index = self._order.index(previous_item_id) + 1
                self._order.insert(index, identifier)
            else:
                self._order.append(identifier)
        self._items[identifier] = payload
        self._trim()
        return copy.deepcopy(payload)

    def retrieve(self, item_id: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._items[item_id])
        except KeyError as exc:
            raise ConversationMutationError(
                f"conversation item 不存在: {item_id}",
                code="item_not_found",
                param="item_id",
            ) from exc

    def delete(self, item_id: str) -> None:
        self.retrieve(item_id)
        del self._items[item_id]
        self._order.remove(item_id)

    def truncate(
        self,
        item_id: str,
        *,
        content_index: int,
        audio_end_ms: int,
    ) -> dict[str, Any]:
        if audio_end_ms < 0:
            raise ConversationMutationError(
                "audio_end_ms 不能为负数",
                code="invalid_value",
                param="audio_end_ms",
            )
        item = self.retrieve(item_id)
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise ConversationMutationError(
                "只能截断 assistant 音频消息",
                code="invalid_item_type",
                param="item_id",
            )
        content = item.get("content") or []
        if content_index < 0 or content_index >= len(content):
            raise ConversationMutationError(
                "content_index 超出范围",
                code="invalid_value",
                param="content_index",
            )
        part = content[content_index]
        if part.get("type") != "output_audio":
            raise ConversationMutationError(
                "指定 content part 不是 output_audio",
                code="invalid_item_type",
                param="content_index",
            )
        # OpenAI removes the unheard transcript after truncating assistant audio.
        part.pop("transcript", None)
        part.pop("audio", None)
        self._items[item_id] = item
        return copy.deepcopy(item)

    def responses_input(self) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for identifier in self._order:
            value = self._project(self._items[identifier])
            if value is not None:
                projected.append(value)
        return projected

    @classmethod
    def project_items(cls, items: list[Any]) -> list[dict[str, Any]]:
        temporary = cls(max_items=max(1, len(items)))
        for item in items:
            temporary.create(item)
        return temporary.responses_input()

    def _trim(self) -> None:
        while len(self._order) > self._max_items:
            identifier = self._order.pop(0)
            self._items.pop(identifier, None)

    @staticmethod
    def _project(item: dict[str, Any]) -> dict[str, Any] | None:
        item_type = item.get("type")
        if item_type == "message":
            role = item.get("role")
            content: list[dict[str, Any]] = []
            for part in item.get("content") or []:
                part_type = part.get("type")
                if part_type in {"input_text", "output_text"}:
                    text = part.get("text")
                    if text is not None:
                        content.append({"type": part_type, "text": str(text)})
                elif part_type == "input_image":
                    content.append(
                        {
                            "type": "input_image",
                            "detail": part.get("detail") or "auto",
                            "image_url": part.get("image_url"),
                        }
                    )
                elif part_type == "input_audio":
                    transcript = part.get("transcript")
                    if transcript:
                        content.append(
                            {"type": "input_text", "text": str(transcript)}
                        )
                    elif part.get("audio"):
                        content.append(
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": _pcm_base64_to_wav(part["audio"]),
                                    "format": "wav",
                                },
                            }
                        )
                elif part_type == "output_audio" and part.get("transcript"):
                    content.append(
                        {
                            "type": "output_text",
                            "text": str(part["transcript"]),
                        }
                    )
            if not content:
                return None
            return {"role": role, "content": content}
        if item_type in {
            "function_call",
            "function_call_output",
            "mcp_approval_response",
            "mcp_list_tools",
            "mcp_call",
            "mcp_approval_request",
        }:
            return {
                key: copy.deepcopy(value)
                for key, value in item.items()
                if key not in {"id", "object", "status"}
            }
        return None


def _pcm_base64_to_wav(value: str) -> str:
    try:
        pcm = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConversationMutationError(
            "input_audio.audio 必须是合法的 base64",
            code="invalid_audio_base64",
            param="item.content.audio",
        ) from exc
    if len(pcm) % 2:
        raise ConversationMutationError(
            "input_audio PCM S16LE 字节数必须为偶数",
            code="invalid_audio",
            param="item.content.audio",
        )
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(pcm)
    return base64.b64encode(target.getvalue()).decode("ascii")
