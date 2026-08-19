from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, cast
from uuid import uuid4

import numpy as np
import soxr

from audio_harness.inferencers.limits import ResizableConcurrencyLimiter
from audio_harness.engines.pool import InferencerPool
from audio_harness.inferencers.responses.http import ResponsesInferencer
from audio_harness.inferencers.tts.base import TtsInferencer
from audio_harness.serving.realtime.conversation import RealtimeConversation
from audio_harness.serving.realtime.tools import (
    McpToolPool,
    ToolRegistry,
    json_tool_output,
)
from audio_harness.voice_agents.models import McpServerConfig
from audio_harness.voice_agents.service import ActiveVoiceAgent

EventSender = Callable[[dict[str, Any]], Awaitable[None]]
AudioSender = Callable[[bytes, int, int], Awaitable[None]]
AudioClearer = Callable[[int], Awaitable[None]]
AudioFinisher = Callable[[int], Awaitable[None]]
VoiceResolver = Callable[[str], Awaitable[TtsInferencer.Voice]]
InterruptReason = Literal["turn_detected", "client_cancelled"]

_TEXT_END = object()
_REALTIME_OUTPUT_SAMPLE_RATE = 24_000


@asynccontextmanager
async def _unlimited_slot():
    yield


@dataclass
class AgentSessionOptions:
    instructions: str = ""
    output_modalities: list[str] = field(default_factory=lambda: ["audio"])
    voice: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = "auto"
    truncation: str = "auto"
    max_output_tokens: int | str | None = None
    reasoning_effort: str | None = None
    parallel_tool_calls: bool = True
    input_audio_format: dict[str, Any] = field(
        default_factory=lambda: {"type": "audio/pcm", "rate": 24_000}
    )
    output_audio_format: dict[str, Any] = field(
        default_factory=lambda: {"type": "audio/pcm", "rate": 24_000}
    )
    turn_detection: dict[str, Any] | None = field(
        default_factory=lambda: {"type": "server_vad"}
    )


@dataclass(frozen=True)
class ResponseRunOptions:
    instructions: str
    output_modalities: list[str]
    voice: str | None
    tools: list[dict[str, Any]]
    tool_choice: Any
    max_output_tokens: int | str
    reasoning_effort: str
    parallel_tool_calls: bool
    output_audio_format: dict[str, Any]
    metadata: dict[str, str] | None
    add_to_conversation: bool


@dataclass(frozen=True)
class _FunctionCall:
    call_id: str
    name: str
    arguments: str
    item_id: str
    output_index: int


@dataclass
class _ResponseOutput:
    response_id: str
    item_id: str
    audio: bool
    add_to_conversation: bool
    previous_item_id: str | None
    text: list[str] = field(default_factory=list)
    function_items: list[dict[str, Any]] = field(default_factory=list)
    started: bool = False
    terminal: bool = False
    item: dict[str, Any] | None = None


class RealtimeAgentRuntime:
    """Responses + 双向流式 TTS 的单会话 Agent 编排器。"""

    def __init__(
        self,
        *,
        profile: ActiveVoiceAgent,
        inferencer_pool: InferencerPool,
        send_event: EventSender,
        send_audio: AudioSender | None,
        clear_audio: AudioClearer | None,
        responses_limiter: ResizableConcurrencyLimiter | None,
        tts_limiter: ResizableConcurrencyLimiter | None,
        tool_registry: ToolRegistry,
        voice_resolver: VoiceResolver | None = None,
        finish_audio: AudioFinisher | None = None,
    ) -> None:
        self._pool = inferencer_pool
        self._send_event = send_event
        self._send_audio = send_audio
        self._clear_audio = clear_audio
        self._responses_limiter = responses_limiter
        self._tts_limiter = tts_limiter
        self._tool_registry = tool_registry
        self._voice_resolver = voice_resolver
        self._finish_audio = finish_audio
        self._response_snapshot = profile.responses_engine
        self._tts_snapshot = profile.tts_engine
        self._response_config = profile.definition.responses
        self._tts_config = profile.definition.tts
        self.options = AgentSessionOptions(
            voice=self._tts_config.default_voice,
            max_output_tokens=self._response_config.max_output_tokens,
            reasoning_effort=(
                "high" if self._response_config.thinking_enabled else "none"
            ),
        )
        self.session_id = f"sess_{uuid4().hex}"
        self.conversation_id = f"conv_{uuid4().hex}"
        self._conversation = RealtimeConversation(
            max_items=self._response_config.history_max_items
        )
        self._history: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None
        self._epoch = 0
        self._response_tasks: dict[str, asyncio.Task[None]] = {}
        self._response_options: dict[str, ResponseRunOptions] = {}
        self._response_epochs: dict[str, int] = {}
        self._response_outputs: dict[str, _ResponseOutput] = {}
        self._terminal_responses: set[str] = set()
        self._response_lock = asyncio.Lock()
        self._conversation_event_lock = asyncio.Lock()
        self._starting_default_response = False
        self._starting_audio_response = False
        self._default_response_id: str | None = None
        self._latest_audio_response: tuple[str, int] | None = None
        self._pending_client_tools: dict[
            str,
            tuple[asyncio.Future[str], bool],
        ] = {}
        self._pending_approvals: dict[
            str,
            tuple[asyncio.Future[bool], bool],
        ] = {}
        self._closed = False
        self._voice_locked = False

    @property
    def active(self) -> bool:
        return any(not task.done() for task in self._response_tasks.values())

    @property
    def active_response_id(self) -> str | None:
        if (
            self._default_response_id is not None
            and self._default_response_id in self._response_tasks
        ):
            return self._default_response_id
        for response_id, options in reversed(
            list(self._response_options.items())
        ):
            if (
                options.output_modalities == ["audio"]
                and response_id in self._response_tasks
            ):
                return response_id
        return None

    @property
    def active_audio_response_id(self) -> str | None:
        for response_id, options in reversed(
            list(self._response_options.items())
        ):
            if (
                options.output_modalities == ["audio"]
                and response_id in self._response_tasks
            ):
                return response_id
        return None

    async def clear_output_audio(self) -> str:
        response_id = self.active_audio_response_id
        epoch = (
            self._response_epochs.get(response_id)
            if response_id is not None
            else None
        )
        if response_id is None and self._latest_audio_response is not None:
            response_id, epoch = self._latest_audio_response
        if response_id is None or epoch is None:
            raise ValueError("当前没有活动响应")
        if self._clear_audio is None:
            raise ValueError("当前传输不支持清空输出音频缓冲区")
        await self._clear_audio(epoch)
        return response_id

    def session_document(self, *, model: str) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "object": "realtime.session",
            "type": "realtime",
            "model": model,
            "instructions": self.options.instructions,
            "output_modalities": self.options.output_modalities,
            "max_output_tokens": self.options.max_output_tokens,
            "audio": {
                "input": {
                    "format": copy.deepcopy(self.options.input_audio_format),
                    "turn_detection": copy.deepcopy(
                        self.options.turn_detection
                    ),
                },
                "output": {
                    "format": copy.deepcopy(self.options.output_audio_format),
                    "voice": self.options.voice,
                },
            },
            "tools": self.options.tools,
            "tool_choice": self.options.tool_choice,
            "truncation": self.options.truncation,
            "parallel_tool_calls": self.options.parallel_tool_calls,
            "reasoning": (
                {"effort": self.options.reasoning_effort}
                if self.options.reasoning_effort not in (None, "none")
                else None
            ),
        }

    def update_options(self, payload: dict[str, Any]) -> None:
        # Validate into an isolated snapshot so a rejected session.update never
        # leaks a partially applied configuration into the live session.
        options = copy.deepcopy(self.options)
        if payload.get("type", "realtime") != "realtime":
            raise ValueError("语音智能体模型只支持 realtime session")
        modalities = payload.get("output_modalities")
        if "output_modalities" in payload:
            if modalities not in (["audio"], ["text"]):
                raise ValueError("output_modalities 只支持 ['audio'] 或 ['text']")
            options.output_modalities = list(modalities)
        if "instructions" in payload and payload["instructions"] is None:
            raise ValueError("清空 instructions 请传空字符串，而不是 null")
        if payload.get("instructions") is not None:
            options.instructions = str(payload["instructions"])
        unsupported = {
            key
            for key in ("include", "prompt", "tracing")
            if payload.get(key) is not None
        }
        if unsupported:
            raise ValueError(
                "当前语音智能体不支持会话配置: "
                + ", ".join(sorted(unsupported))
            )
        if "max_output_tokens" in payload:
            requested_max = payload["max_output_tokens"]
            options.max_output_tokens = (
                self._response_config.max_output_tokens
                if requested_max is None
                else self._validate_max_output_tokens(requested_max)
            )
        if (
            "parallel_tool_calls" in payload
            and payload["parallel_tool_calls"] is None
        ):
            raise ValueError("parallel_tool_calls 不能为 null")
        if payload.get("parallel_tool_calls") is not None:
            options.parallel_tool_calls = bool(
                payload["parallel_tool_calls"]
            )
        reasoning = payload.get("reasoning")
        if "reasoning" in payload and reasoning is None:
            options.reasoning_effort = (
                "high" if self._response_config.thinking_enabled else "none"
            )
        elif reasoning is not None:
            effort = reasoning.get("effort")
            if effort not in {None, "minimal", "low", "medium", "high", "xhigh"}:
                raise ValueError(f"不支持的 reasoning effort: {effort}")
            options.reasoning_effort = effort or "none"
        audio_input = ((payload.get("audio") or {}).get("input") or {})
        if audio_input.get("noise_reduction") is not None:
            raise ValueError("本地语音工作流不支持 noise_reduction 配置")
        if audio_input.get("transcription") is not None:
            raise ValueError("本地语音工作流的转写模型由公开路由固定")
        if "format" in audio_input:
            if audio_input["format"] is None:
                raise ValueError("audio.input.format 不能为 null")
            options.input_audio_format = self._validate_input_audio_format(
                audio_input["format"]
            )
        if "turn_detection" in audio_input:
            options.turn_detection = self._validate_turn_detection(
                audio_input["turn_detection"]
            )
        audio_output = ((payload.get("audio") or {}).get("output") or {})
        if audio_output.get("speed") not in (None, 1, 1.0):
            raise ValueError("本地 TTS 暂不支持修改 speed")
        if "format" in audio_output:
            if audio_output["format"] is None:
                raise ValueError("audio.output.format 不能为 null")
            options.output_audio_format = self._validate_output_audio_format(
                audio_output["format"]
            )
        if "voice" in audio_output and audio_output["voice"] is None:
            raise ValueError("audio.output.voice 不能为 null")
        if audio_output.get("voice") is not None:
            voice = str(audio_output["voice"])
            if self._voice_locked and voice != options.voice:
                raise ValueError("会话已经输出音频，不能再修改音色")
            if voice not in self._tts_config.allowed_voices:
                raise ValueError(f"工作流不允许音色: {voice}")
            options.voice = voice
        tools = payload.get("tools")
        if "tools" in payload and tools is None:
            raise ValueError("清空 tools 请传空数组，而不是 null")
        if tools is not None:
            values = []
            for tool in tools:
                if tool.get("type") not in {"function", "mcp"}:
                    raise ValueError("客户端工具只支持 function 或 mcp")
                values.append(dict(tool))
            options.tools = values
        if "tool_choice" in payload and payload["tool_choice"] is None:
            raise ValueError("tool_choice 不能为 null")
        if payload.get("tool_choice") is not None:
            options.tool_choice = payload["tool_choice"]
        truncation = payload.get("truncation")
        if truncation is not None:
            if not isinstance(truncation, str) or truncation not in {
                "auto",
                "disabled",
            }:
                raise ValueError("truncation 只支持 auto 或 disabled")
            options.truncation = truncation
        self.options = options

    def _validate_max_output_tokens(self, value: Any) -> int | str:
        configured = self._response_config.max_output_tokens
        if value == "inf":
            if configured != "inf":
                raise ValueError(
                    "max_output_tokens 不能超过语音智能体工作流上限"
                )
            return value
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("max_output_tokens 必须是正整数或 inf")
        if isinstance(configured, int) and value > configured:
            raise ValueError(
                "max_output_tokens 不能超过语音智能体工作流上限"
            )
        return value

    @staticmethod
    def _audio_format_payload(value: Any) -> dict[str, Any]:
        return (
            value.model_dump(mode="json", exclude_none=True)
            if hasattr(value, "model_dump")
            else dict(value)
        )

    @classmethod
    def _validate_input_audio_format(cls, value: Any) -> dict[str, Any]:
        payload = cls._audio_format_payload(value)
        format_type = payload.get("type")
        if format_type == "audio/pcm":
            sample_rate = payload.get("rate", 24_000)
            if sample_rate not in {16_000, 24_000}:
                raise ValueError("audio/pcm 输入只支持 16 kHz 或 24 kHz")
            return {"type": "audio/pcm", "rate": sample_rate}
        raise ValueError(f"不支持的音频格式（输入）: {format_type}")

    @classmethod
    def _validate_output_audio_format(cls, value: Any) -> dict[str, Any]:
        payload = cls._audio_format_payload(value)
        format_type = payload.get("type")
        if format_type == "audio/pcm":
            if payload.get("rate", 24_000) != 24_000:
                raise ValueError("audio/pcm 输出只支持 24 kHz")
            return {"type": "audio/pcm", "rate": 24_000}
        raise ValueError(f"不支持的音频格式（输出）: {format_type}")

    @staticmethod
    def _validate_turn_detection(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        payload = (
            value.model_dump(mode="json", exclude_none=True)
            if hasattr(value, "model_dump")
            else dict(value)
        )
        if payload.get("type") not in {"server_vad", "semantic_vad"}:
            raise ValueError("turn_detection 只支持 server_vad、semantic_vad 或 null")
        return payload

    def create_conversation_item(
        self,
        item: Any,
        *,
        previous_item_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        if (
            len(self._conversation) >= self._response_config.history_max_items
            and self.options.truncation == "disabled"
        ):
            raise ValueError(
                "会话历史超过工作流上限，且 truncation 已禁用"
            )
        payload = (
            item.model_dump(mode="json", exclude_none=True)
            if hasattr(item, "model_dump")
            else copy.deepcopy(item)
        )
        self._normalize_input_audio(payload)
        created = self._conversation.create(
            payload,
            previous_item_id=previous_item_id,
        )
        self._history = self._conversation.responses_input()
        return created

    def _normalize_input_audio(self, item: dict[str, Any]) -> None:
        if item.get("type") != "message" or item.get("role") != "user":
            return
        for part in item.get("content") or []:
            if part.get("type") != "input_audio" or not part.get("audio"):
                continue
            try:
                wire_audio = base64.b64decode(part["audio"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(
                    "input_audio.audio 必须是合法的 base64"
                ) from exc
            if len(wire_audio) % 2:
                raise ValueError("input_audio.audio 必须是 PCM S16LE")
            part["audio"] = base64.b64encode(wire_audio).decode("ascii")

    def retrieve_conversation_item(self, item_id: str) -> dict[str, Any]:
        return self._conversation.retrieve(item_id)

    def delete_conversation_item(self, item_id: str) -> None:
        self._conversation.delete(item_id)
        self._history = self._conversation.responses_input()

    def truncate_conversation_item(
        self,
        item_id: str,
        *,
        content_index: int,
        audio_end_ms: int,
    ) -> dict[str, Any]:
        item = self._conversation.truncate(
            item_id,
            content_index=content_index,
            audio_end_ms=audio_end_ms,
        )
        self._history = self._conversation.responses_input()
        return item

    def add_transcribed_audio(self, item_id: str, transcript: str) -> None:
        self._conversation.upsert_completed(
            {
                "id": item_id,
                "object": "realtime.item",
                "type": "message",
                "role": "user",
                "status": "completed",
                "content": [
                    {"type": "input_audio", "transcript": transcript}
                ],
            }
        )
        self._history = self._conversation.responses_input()

    async def respond(self, transcript: str, *, input_item_id: str) -> None:
        if self._closed:
            return
        if self._default_response_id is not None:
            await self.interrupt(
                reason="turn_detected",
                emit=True,
                response_id=self._default_response_id,
            )
        self.add_transcribed_audio(input_item_id, transcript)
        self._truncate_history()
        await self._start_response(
            self._snapshot_response_options(None, add_to_conversation=True),
        )

    async def create_response(self, response: Any | None) -> str:
        if self._closed:
            raise ValueError("Realtime Agent 会话已经关闭")
        payload = (
            response.model_dump(
                mode="json",
                exclude_none=True,
                exclude_unset=True,
            )
            if response is not None and hasattr(response, "model_dump")
            else dict(response or {})
        )
        conversation = payload.get("conversation", "auto")
        if conversation not in {"auto", "none", self.conversation_id}:
            raise ValueError(f"conversation 不存在: {conversation}")
        add_to_conversation = conversation != "none"
        history = (
            self._project_response_input(payload["input"])
            if "input" in payload
            else self._conversation.responses_input()
        )
        options = self._snapshot_response_options(
            payload,
            add_to_conversation=add_to_conversation,
        )
        return await self._start_response(
            options,
            history=history,
        )

    def _project_response_input(self, items: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            payload = (
                item.model_dump(mode="json", exclude_none=True)
                if hasattr(item, "model_dump")
                else copy.deepcopy(item)
            )
            self._normalize_input_audio(payload)
            normalized.append(payload)
        return RealtimeConversation.project_items(normalized)

    async def _start_response(
        self,
        options: ResponseRunOptions,
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> str:
        audio_response = options.output_modalities == ["audio"]
        async with self._response_lock:
            if self._closed:
                raise ValueError("Realtime Agent 会话已经关闭")
            if options.add_to_conversation and (
                self._starting_default_response
                or self._default_response_id in self._response_tasks
            ):
                raise ValueError("默认 conversation 已经有活动响应")
            if audio_response and (
                self._starting_audio_response
                or any(
                    current.output_modalities == ["audio"]
                    for current in self._response_options.values()
                )
            ):
                raise ValueError("同一会话一次只能生成一个音频响应")
            if options.add_to_conversation:
                self._starting_default_response = True
            if audio_response:
                self._starting_audio_response = True
            self._epoch += 1
            epoch = self._epoch
            response_id = f"resp_{uuid4().hex}"

        try:
            if audio_response and self._clear_audio is not None:
                # ServerAudioTrack uses the response epoch to discard audio
                # produced by an older, cancelled response.
                await self._clear_audio(epoch)

            async with self._response_lock:
                if self._closed:
                    raise ValueError("Realtime Agent 会话已经关闭")
                output_state = _ResponseOutput(
                    response_id=response_id,
                    item_id=f"item_{uuid4().hex}",
                    audio=audio_response,
                    add_to_conversation=options.add_to_conversation,
                    previous_item_id=(
                        self._conversation.last_item_id
                        if options.add_to_conversation
                        else None
                    ),
                )
                self._response_outputs[response_id] = output_state
                try:
                    await self._send_event(
                        {
                            "type": "response.created",
                            "event_id": f"event_{uuid4().hex}",
                            "response": self._response_document(
                                response_id=response_id,
                                status="in_progress",
                                options=options,
                                output=[],
                            ),
                        }
                    )
                    task = asyncio.create_task(
                        self._run_response(
                            epoch,
                            response_id=response_id,
                            options=options,
                            output_state=output_state,
                            history=list(
                                history if history is not None else self._history
                            ),
                        ),
                        name=f"realtime-agent-response-{epoch}",
                    )
                except BaseException:
                    self._response_outputs.pop(response_id, None)
                    raise
                self._task = task
                self._response_tasks[response_id] = task
                self._response_options[response_id] = options
                self._response_epochs[response_id] = epoch
                if options.add_to_conversation:
                    self._default_response_id = response_id
                if audio_response:
                    self._latest_audio_response = (response_id, epoch)
        finally:
            async with self._response_lock:
                if options.add_to_conversation:
                    self._starting_default_response = False
                if audio_response:
                    self._starting_audio_response = False
        return response_id

    def _snapshot_response_options(
        self,
        payload: dict[str, Any] | None,
        *,
        add_to_conversation: bool,
    ) -> ResponseRunOptions:
        values = payload or {}
        modalities = values.get("output_modalities") or list(
            self.options.output_modalities
        )
        if modalities not in (["audio"], ["text"]):
            raise ValueError("output_modalities 只支持 ['audio'] 或 ['text']")
        unsupported = {
            key for key in ("prompt",) if values.get(key) is not None
        }
        if unsupported:
            raise ValueError("response.create 暂不支持 prompt")
        tools = (
            [dict(tool) for tool in values["tools"]]
            if values.get("tools") is not None
            else copy.deepcopy(self.options.tools)
        )
        for tool in tools:
            if tool.get("type") not in {"function", "mcp"}:
                raise ValueError("客户端工具只支持 function 或 mcp")
        maximum = self._validate_max_output_tokens(
            values.get("max_output_tokens")
            or self.options.max_output_tokens
            or self._response_config.max_output_tokens
        )
        reasoning = values.get("reasoning") or {}
        effort = reasoning.get("effort") or self.options.reasoning_effort or "none"
        if effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError(f"不支持的 reasoning effort: {effort}")
        audio_output = ((values.get("audio") or {}).get("output") or {})
        output_format = (
            self._validate_output_audio_format(audio_output["format"])
            if audio_output.get("format") is not None
            else copy.deepcopy(self.options.output_audio_format)
        )
        voice = audio_output.get("voice") or self.options.voice
        if voice is not None:
            if isinstance(voice, dict):
                voice = voice.get("id")
            voice = str(voice)
            if voice not in self._tts_config.allowed_voices:
                raise ValueError(f"工作流不允许音色: {voice}")
        return ResponseRunOptions(
            instructions=str(
                values.get("instructions", self.options.instructions)
            ),
            output_modalities=list(modalities),
            voice=voice,
            tools=tools,
            tool_choice=values.get("tool_choice", self.options.tool_choice),
            max_output_tokens=maximum,
            reasoning_effort=effort,
            parallel_tool_calls=bool(
                values.get(
                    "parallel_tool_calls",
                    self.options.parallel_tool_calls,
                )
            ),
            output_audio_format=output_format,
            metadata=(
                dict(values["metadata"])
                if values.get("metadata") is not None
                else None
            ),
            add_to_conversation=add_to_conversation,
        )

    def _response_document(
        self,
        *,
        response_id: str,
        status: str,
        options: ResponseRunOptions,
        output: list[dict[str, Any]],
        status_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": response_id,
            "object": "realtime.response",
            "status": status,
            "status_details": status_details,
            "output": output,
            "conversation_id": (
                self.conversation_id
                if options.add_to_conversation
                else None
            ),
            "output_modalities": options.output_modalities,
            "max_output_tokens": options.max_output_tokens,
            "audio": {
                "output": {
                    "format": options.output_audio_format,
                    "voice": options.voice,
                }
            },
            "usage": None,
            "metadata": options.metadata,
        }

    async def _finish_output(
        self,
        state: _ResponseOutput,
        *,
        status: str,
    ) -> dict[str, Any] | None:
        if not state.started:
            return None
        if state.terminal:
            return state.item
        state.terminal = True
        self._terminal_responses.add(state.response_id)
        item = await self._emit_output_done(
            response_id=state.response_id,
            item_id=state.item_id,
            text="".join(state.text),
            audio=state.audio,
            previous_item_id=state.previous_item_id,
            status=status,
        )
        state.item = item
        if state.add_to_conversation:
            self._conversation.upsert(item)
            self._history = self._conversation.responses_input()
        return item

    @staticmethod
    def _collected_output(
        state: _ResponseOutput,
        message: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        return ([message] if message is not None else []) + list(
            state.function_items
        )

    async def interrupt(
        self,
        *,
        reason: InterruptReason = "turn_detected",
        emit: bool = True,
        response_id: str | None = None,
    ) -> None:
        terminal_task: asyncio.Task[None] | None = None
        async with self._response_lock:
            active_response_id = response_id or self._default_response_id
            if active_response_id is None:
                if emit:
                    raise ValueError("当前没有活动响应")
                return
            task = self._response_tasks.get(active_response_id)
            options = self._response_options.get(active_response_id)
            response_epoch = self._response_epochs.get(
                active_response_id,
                self._epoch,
            )
            if task is None or options is None or task.done():
                self._forget_response(active_response_id)
                if emit:
                    raise ValueError(
                        f"活动响应不存在: {active_response_id}"
                    )
                return
            if active_response_id in self._terminal_responses:
                terminal_task = task
            else:
                # Claim the response before yielding so concurrent cancel
                # requests cannot emit duplicate terminal events.
                self._forget_response(active_response_id)

        if terminal_task is not None:
            await asyncio.gather(terminal_task, return_exceptions=True)
            if emit:
                raise ValueError(f"活动响应不存在: {active_response_id}")
            return

        if not task.cancel():
            if emit:
                raise ValueError(f"活动响应不存在: {active_response_id}")
            return
        await asyncio.gather(task, return_exceptions=True)
        try:
            if emit:
                output_state = self._response_outputs.get(active_response_id)
                assert output_state is not None
                partial_item = await self._finish_output(
                    output_state,
                    status="incomplete",
                )
                self._terminal_responses.add(active_response_id)
                await self._send_event(
                    {
                        "type": "response.done",
                        "event_id": f"event_{uuid4().hex}",
                        "response": self._response_document(
                            response_id=active_response_id,
                            status="cancelled",
                            options=options,
                            output=self._collected_output(
                                output_state,
                                partial_item,
                            ),
                            status_details={
                                "type": "cancelled",
                                "reason": reason,
                            },
                        ),
                    }
                )
        finally:
            self._terminal_responses.discard(active_response_id)
            self._response_outputs.pop(active_response_id, None)
        should_clear_audio = reason != "client_cancelled"
        if (
            should_clear_audio
            and self._clear_audio is not None
            and options is not None
            and options.output_modalities == ["audio"]
        ):
            await self._clear_audio(response_epoch)
            if emit:
                await self._send_event(
                    {
                        "type": "output_audio_buffer.cleared",
                        "event_id": f"event_{uuid4().hex}",
                        "response_id": active_response_id,
                    }
                )

    async def close(self) -> None:
        async with self._response_lock:
            if self._closed:
                return
            self._closed = True
            response_ids = list(self._response_tasks)
        for response_id in response_ids:
            await self.interrupt(response_id=response_id, emit=False)

    def _forget_response(self, response_id: str) -> None:
        task = self._response_tasks.pop(response_id, None)
        self._response_options.pop(response_id, None)
        self._response_epochs.pop(response_id, None)
        if self._default_response_id == response_id:
            self._default_response_id = None
        if self._task is task:
            self._task = None

    async def submit_function_output(self, call_id: str, output: str) -> bool:
        pending = self._pending_client_tools.pop(call_id, None)
        if pending is None:
            raise ValueError(f"不存在待处理的 function call: {call_id}")
        future, add_to_conversation = pending
        if not future.done():
            future.set_result(output)
        return add_to_conversation

    async def submit_mcp_approval(self, request_id: str, approve: bool) -> bool:
        pending = self._pending_approvals.pop(request_id, None)
        if pending is None:
            raise ValueError(f"不存在待审批的 MCP 调用: {request_id}")
        future, add_to_conversation = pending
        if not future.done():
            future.set_result(approve)
        return add_to_conversation

    def _truncate_history(self) -> None:
        maximum = self._response_config.history_max_items
        if len(self._history) <= maximum:
            return
        if self.options.truncation == "disabled":
            raise RuntimeError(
                "会话历史超过工作流上限，且 truncation 已禁用"
            )
        while len(self._history) > maximum:
            next_turn = next(
                (
                    index
                    for index, item in enumerate(self._history[1:], start=1)
                    if item.get("role") == "user"
                ),
                None,
            )
            if next_turn is None:
                break
            del self._history[:next_turn]

    async def _run_response(
        self,
        epoch: int,
        *,
        response_id: str,
        options: ResponseRunOptions,
        output_state: _ResponseOutput,
        history: list[dict[str, Any]],
    ) -> None:
        output_item_id = output_state.item_id
        try:
            response_slot = (
                self._responses_limiter.try_slot()
                if self._responses_limiter is not None
                else _unlimited_slot()
            )
            audio_output = options.output_modalities == ["audio"]
            tts_slot = (
                self._tts_limiter.try_slot()
                if audio_output and self._tts_limiter is not None
                else _unlimited_slot()
            )
            async with response_slot, tts_slot:
                async with self._pool.lease(
                    (self._response_snapshot, self._tts_snapshot)
                    if audio_output
                    else (self._response_snapshot,)
                ) as leased:
                    responses = cast(
                        ResponsesInferencer,
                        leased[
                            (
                                self._response_snapshot.id,
                                self._response_snapshot.revision,
                            )
                        ],
                    )
                    tts = (
                        cast(
                            TtsInferencer,
                            leased[
                                (
                                    self._tts_snapshot.id,
                                    self._tts_snapshot.revision,
                                )
                            ],
                        )
                        if audio_output
                        else None
                    )
                    completed_item = await self._stream_turn(
                        epoch=epoch,
                        response_id=response_id,
                        output_item_id=output_item_id,
                        output_state=output_state,
                        responses=responses,
                        tts=tts,
                        options=options,
                        history=history,
                    )
            await self._send_event(
                {
                    "type": "response.done",
                    "event_id": f"event_{uuid4().hex}",
                    "response": self._response_document(
                        response_id=response_id,
                        status="completed",
                        options=options,
                        output=self._collected_output(
                            output_state,
                            completed_item,
                        ),
                    ),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            partial_item = await self._finish_output(
                output_state,
                status="incomplete",
            )
            self._terminal_responses.add(response_id)
            await self._send_event(
                {
                    "type": "error",
                    "event_id": f"event_{uuid4().hex}",
                    "error": {
                        "type": "server_error",
                        "code": "agent_response_failed",
                        "message": str(exc),
                    },
                }
            )
            await self._send_event(
                {
                    "type": "response.done",
                    "event_id": f"event_{uuid4().hex}",
                    "response": self._response_document(
                        response_id=response_id,
                        status="failed",
                        options=options,
                        output=self._collected_output(
                            output_state,
                            partial_item,
                        ),
                    ),
                }
            )
        finally:
            self._forget_response(response_id)
            if response_id in self._terminal_responses:
                self._terminal_responses.discard(response_id)
                self._response_outputs.pop(response_id, None)

    async def _stream_turn(
        self,
        *,
        epoch: int,
        response_id: str,
        output_item_id: str,
        output_state: _ResponseOutput,
        responses: ResponsesInferencer,
        tts: TtsInferencer | None,
        options: ResponseRunOptions,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        mcp_configs = list(self._response_config.mcp_servers)
        memory_headers: dict[str, dict[str, str]] = {}
        for tool in options.tools:
            if tool.get("type") != "mcp":
                continue
            label = str(tool.get("server_label") or "").strip()
            url = str(tool.get("server_url") or "").strip()
            if not label or not url:
                raise ValueError(
                    "客户端 MCP 必须包含 server_label 和 server_url"
                )
            allowed = tool.get("allowed_tools")
            if isinstance(allowed, dict):
                allowed = allowed.get("tool_names")
            mcp_configs.append(
                McpServerConfig(
                    server_label=label,
                    server_url=url,
                    require_approval=tool.get("require_approval") or "always",
                    allowed_tools=allowed,
                )
            )
            headers = dict(tool.get("headers") or {})
            if tool.get("authorization"):
                headers["Authorization"] = str(tool["authorization"])
            memory_headers[label] = headers
        mcp = McpToolPool(mcp_configs, memory_headers=memory_headers)
        tool_definitions = self._tool_registry.definitions(
            self._response_config.service_tools
        )
        tool_definitions.extend(await mcp.definitions())
        client_tool_names: set[str] = set()
        for tool in options.tools:
            if tool.get("type") == "function":
                tool_definitions.append(tool)
                if tool.get("name"):
                    client_tool_names.add(str(tool["name"]))
        instructions = self._response_config.instructions
        if options.instructions:
            instructions += "\n\n" + options.instructions
        tool_choice, constraint = self._tool_choice(options)
        if constraint:
            instructions += "\n\n" + constraint

        # Responses 的输出受 max_output_tokens 限制。使用无界队列可避免
        # TTS 提前失败后生产者卡在已经无人消费的有界队列上。
        text_queue: asyncio.Queue[str | object] = asyncio.Queue()
        full_text = output_state.text
        next_function_output_index = 1
        previous_item_id = output_state.previous_item_id
        start_task = asyncio.create_task(
            self._emit_output_started(
                response_id=response_id,
                item_id=output_item_id,
                audio=tts is not None,
                add_to_conversation=options.add_to_conversation,
                previous_item_id=previous_item_id,
            ),
            name=f"realtime-agent-output-start-{response_id}",
        )
        try:
            await asyncio.shield(start_task)
        except asyncio.CancelledError:
            await asyncio.gather(start_task, return_exceptions=True)
            if not start_task.cancelled() and start_task.exception() is None:
                output_state.started = True
            raise
        else:
            output_state.started = True
        audio_task = (
            asyncio.create_task(
                self._stream_tts(
                    epoch=epoch,
                    response_id=response_id,
                    output_item_id=output_item_id,
                    tts=tts,
                    text_queue=text_queue,
                    voice=options.voice,
                )
            )
            if tts is not None
            else None
        )
        try:
            for round_index in range(self._response_config.max_tool_rounds):
                calls = await self._responses_round(
                    responses=responses,
                    instructions=instructions,
                    tools=tool_definitions,
                    tool_choice=tool_choice,
                    response_id=response_id,
                    output_item_id=output_item_id,
                    text_queue=text_queue,
                    full_text=full_text,
                    audio=tts is not None,
                    function_output_index=next_function_output_index,
                    options=options,
                    output_state=output_state,
                    history=history,
                )
                next_function_output_index += len(calls)
                if not calls:
                    if constraint and round_index == 0:
                        instructions += (
                            "\n必须调用指定工具；上一轮未调用，"
                            "请立即重试一次。"
                        )
                        continue
                    break
                await self._execute_calls(
                    calls,
                    client_tool_names=client_tool_names,
                    mcp=mcp,
                    history=history,
                    add_to_conversation=options.add_to_conversation,
                )
            else:
                raise RuntimeError("工具调用轮数超过工作流上限")
        except BaseException:
            # 中断时不能等待下游 TTS 自然收尾。某些双工服务在输入流
            # half-close 后仍可能迟迟不结束；若在这里等待，会让当前响应
            # 持有唯一的 TTS 并发槽，进而阻塞下一次用户发言。
            if audio_task is not None:
                audio_task.cancel()
                await asyncio.gather(audio_task, return_exceptions=True)
            raise
        else:
            await text_queue.put(_TEXT_END)
            if audio_task is not None:
                await audio_task
        text = "".join(full_text)
        if text:
            history.append(
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            )
        completed_item = await self._finish_output(
            output_state,
            status="completed",
        )
        assert completed_item is not None
        return completed_item

    def _tool_choice(
        self,
        options: ResponseRunOptions,
    ) -> tuple[Any, str | None]:
        choice = options.tool_choice
        if choice == "required":
            return "auto", "本轮必须调用至少一个可用工具。"
        if isinstance(choice, dict):
            name = choice.get("name") or choice.get("function", {}).get("name")
            if name:
                return (
                    "auto",
                    f"本轮必须调用工具 {name}，且不要调用其他工具。",
                )
        return choice or "auto", None

    async def _responses_round(
        self,
        *,
        responses: ResponsesInferencer,
        instructions: str,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        response_id: str,
        output_item_id: str,
        text_queue: asyncio.Queue[str | object],
        full_text: list[str],
        audio: bool,
        function_output_index: int,
        options: ResponseRunOptions,
        output_state: _ResponseOutput,
        history: list[dict[str, Any]],
    ) -> list[_FunctionCall]:
        kwargs: dict[str, Any] = {
            "model": self._response_config.model,
            "instructions": instructions,
            "input": list(history),
            "max_output_tokens": options.max_output_tokens,
            "reasoning": {
                "effort": options.reasoning_effort
            },
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
            kwargs["parallel_tool_calls"] = options.parallel_tool_calls
        stream = await responses.stream_response(**kwargs)
        calls: list[_FunctionCall] = []
        try:
            async for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    delta = event.delta
                    await self._send_event(
                        {
                            "type": (
                                "response.output_audio_transcript.delta"
                                if audio
                                else "response.output_text.delta"
                            ),
                            "event_id": f"event_{uuid4().hex}",
                            "response_id": response_id,
                            "item_id": output_item_id,
                            "output_index": 0,
                            "content_index": 0,
                            "delta": delta,
                        }
                    )
                    full_text.append(delta)
                    if audio:
                        await text_queue.put(delta)
                elif event_type == "response.output_item.done":
                    item = event.item
                    item_type = getattr(item, "type", "")
                    if item_type == "function_call":
                        call = _FunctionCall(
                            call_id=item.call_id,
                            name=item.name,
                            arguments=item.arguments,
                            item_id=item.id or f"item_{uuid4().hex}",
                            output_index=(
                                function_output_index + len(calls)
                            ),
                        )
                        calls.append(call)
                        history.append(
                            item.model_dump(mode="json", exclude_none=True)
                            if hasattr(item, "model_dump")
                            else {
                                "type": "function_call",
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments": call.arguments,
                            }
                        )
                        completed_call = await self._emit_function_call(
                            call,
                            response_id=response_id,
                            add_to_conversation=options.add_to_conversation,
                        )
                        output_state.function_items.append(completed_call)
                    elif item_type == "reasoning" and hasattr(
                        item,
                        "model_dump",
                    ):
                        history.append(
                            item.model_dump(mode="json", exclude_none=True)
                        )
                elif event_type in {
                    "response.failed",
                    "response.incomplete",
                    "error",
                }:
                    detail = getattr(event, "error", None) or getattr(
                        getattr(event, "response", None),
                        "error",
                        None,
                    )
                    message = getattr(detail, "message", None) or str(detail)
                    raise RuntimeError(
                        f"Responses API 流式响应失败: {message}"
                    )
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                await close()
        return calls

    async def _execute_calls(
        self,
        calls: list[_FunctionCall],
        *,
        client_tool_names: set[str],
        mcp: McpToolPool,
        history: list[dict[str, Any]],
        add_to_conversation: bool,
    ) -> None:
        semaphore = asyncio.Semaphore(self._response_config.max_parallel_tools)

        async def execute(call: _FunctionCall) -> None:
            async with semaphore:
                try:
                    arguments = json.loads(call.arguments or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("工具参数必须是 JSON 对象")
                    if call.name in client_tool_names:
                        future = asyncio.get_running_loop().create_future()
                        self._pending_client_tools[call.call_id] = (
                            future,
                            add_to_conversation,
                        )
                        try:
                            output = await asyncio.wait_for(
                                asyncio.shield(future),
                                timeout=self._response_config.tool_timeout_seconds,
                            )
                        finally:
                            self._pending_client_tools.pop(call.call_id, None)
                            if not future.done():
                                future.cancel()
                        history.append(
                            {
                                "type": "function_call_output",
                                "call_id": call.call_id,
                                "output": output,
                            }
                        )
                        return
                    mcp_tool = mcp.tool(call.name)
                    if mcp_tool is not None:
                        if mcp_tool.approval_required():
                            request_id = f"approval_{uuid4().hex}"
                            future = asyncio.get_running_loop().create_future()
                            self._pending_approvals[request_id] = (
                                future,
                                add_to_conversation,
                            )
                            try:
                                approval_item = {
                                    "id": request_id,
                                    "object": "realtime.item",
                                    "status": "completed",
                                    "type": "mcp_approval_request",
                                    "server_label": mcp_tool.server_label,
                                    "name": mcp_tool.remote_name,
                                    "arguments": call.arguments,
                                }
                                await self._emit_approval_request(
                                    approval_item,
                                    add_to_conversation=add_to_conversation,
                                )
                                approved = await asyncio.wait_for(
                                    asyncio.shield(future),
                                    timeout=(
                                        self._response_config.tool_timeout_seconds
                                    ),
                                )
                            finally:
                                self._pending_approvals.pop(request_id, None)
                                if not future.done():
                                    future.cancel()
                            if not approved:
                                raise RuntimeError("MCP 工具调用未获批准")
                        output = await asyncio.wait_for(
                            mcp.call(call.name, arguments),
                            timeout=self._response_config.tool_timeout_seconds,
                        )
                    else:
                        output = json_tool_output(
                            await asyncio.wait_for(
                                self._tool_registry.call(call.name, arguments),
                                timeout=self._response_config.tool_timeout_seconds,
                            )
                        )
                    history.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": output,
                        }
                    )
                    await self._emit_tool_output(
                        call.call_id,
                        output,
                        add_to_conversation=add_to_conversation,
                    )
                except asyncio.CancelledError:
                    raise
        tasks = [
            asyncio.create_task(
                execute(call),
                name=f"realtime-agent-tool-{call.call_id}",
            )
            for call in calls
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _stream_tts(
        self,
        *,
        epoch: int,
        response_id: str,
        output_item_id: str,
        tts: TtsInferencer,
        text_queue: asyncio.Queue[str | object],
        voice: str | None,
    ) -> None:
        async def chunks() -> AsyncIterator[str]:
            while True:
                item = await text_queue.get()
                if item is _TEXT_END:
                    return
                assert isinstance(item, str)
                yield item

        voice = voice or self._tts_config.default_voice
        selection = (
            await self._voice_resolver(voice)
            if self._voice_resolver is not None
            else voice
        )
        wss_sample_rate: int | None = None
        wss_resampler: soxr.ResampleStream | None = None

        async def send_wss_audio(pcm: bytes) -> None:
            if not pcm:
                return
            self._voice_locked = True
            await self._send_event(
                {
                    "type": "response.output_audio.delta",
                    "event_id": f"event_{uuid4().hex}",
                    "response_id": response_id,
                    "item_id": output_item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": base64.b64encode(pcm).decode("ascii"),
                }
            )

        async for chunk in tts.duplex_synthesize(
            chunks(),
            voice=selection,
            language=self._tts_config.language,
            decoder_chunk_size=self._tts_config.decoder_chunk_size,
        ):
            if self._send_audio is not None:
                self._voice_locked = True
                await self._send_audio(chunk.pcm_s16le, chunk.sample_rate, epoch)
            else:
                if wss_sample_rate is None:
                    wss_sample_rate = chunk.sample_rate
                    if chunk.sample_rate != _REALTIME_OUTPUT_SAMPLE_RATE:
                        wss_resampler = soxr.ResampleStream(
                            chunk.sample_rate,
                            _REALTIME_OUTPUT_SAMPLE_RATE,
                            1,
                            dtype="int16",
                            quality="HQ",
                        )
                elif chunk.sample_rate != wss_sample_rate:
                    raise RuntimeError("TTS 在同一响应中改变了采样率")
                pcm = chunk.pcm_s16le
                if wss_resampler is not None:
                    samples = np.frombuffer(pcm, dtype="<i2")
                    pcm = wss_resampler.resample_chunk(
                        samples,
                        last=False,
                    ).astype("<i2", copy=False).tobytes()
                await send_wss_audio(pcm)
        if wss_resampler is not None:
            tail = wss_resampler.resample_chunk(
                np.empty(0, dtype=np.int16),
                last=True,
            ).astype("<i2", copy=False).tobytes()
            await send_wss_audio(tail)
        if self._finish_audio is not None:
            await self._finish_audio(epoch)

    async def _emit_output_started(
        self,
        *,
        response_id: str,
        item_id: str,
        audio: bool,
        add_to_conversation: bool,
        previous_item_id: str | None,
    ) -> None:
        item = {
            "id": item_id,
            "object": "realtime.item",
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
        await self._send_event(
            {
                "type": "response.output_item.added",
                "event_id": f"event_{uuid4().hex}",
                "response_id": response_id,
                "output_index": 0,
                "item": item,
            }
        )
        if add_to_conversation:
            await self._send_event(
                {
                    "type": "conversation.item.added",
                    "event_id": f"event_{uuid4().hex}",
                    "previous_item_id": previous_item_id,
                    "item": item,
                }
            )
        await self._send_event(
            {
                "type": "response.content_part.added",
                "event_id": f"event_{uuid4().hex}",
                "response_id": response_id,
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "part": {
                    "type": "audio" if audio else "text",
                    "transcript": "" if audio else None,
                    "text": None if audio else "",
                },
            }
        )

    async def _emit_output_done(
        self,
        *,
        response_id: str,
        item_id: str,
        text: str,
        audio: bool,
        previous_item_id: str | None,
        status: str = "completed",
    ) -> dict[str, Any]:
        done_type = (
            "response.output_audio_transcript.done"
            if audio
            else "response.output_text.done"
        )
        await self._send_event(
            {
                "type": done_type,
                "event_id": f"event_{uuid4().hex}",
                "response_id": response_id,
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "transcript" if audio else "text": text,
            }
        )
        if audio:
            await self._send_event(
                {
                    "type": "response.output_audio.done",
                    "event_id": f"event_{uuid4().hex}",
                    "response_id": response_id,
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                }
            )
        part = {
            "type": "audio" if audio else "text",
            "transcript": text if audio else None,
            "text": None if audio else text,
        }
        item = {
            "id": item_id,
            "object": "realtime.item",
            "type": "message",
            "role": "assistant",
            "status": status,
            "content": [
                {
                    "type": "output_audio" if audio else "output_text",
                    "transcript": text if audio else None,
                    "text": None if audio else text,
                }
            ],
        }
        await self._send_event(
            {
                "type": "response.content_part.done",
                "event_id": f"event_{uuid4().hex}",
                "response_id": response_id,
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "part": part,
            }
        )
        await self._send_event(
            {
                "type": "conversation.item.done",
                "event_id": f"event_{uuid4().hex}",
                "previous_item_id": None,
                "item": item,
            }
        )
        await self._send_event(
            {
                "type": "response.output_item.done",
                "event_id": f"event_{uuid4().hex}",
                "response_id": response_id,
                "output_index": 0,
                "item": item,
            }
        )
        return item

    async def _emit_function_call(
        self,
        call: _FunctionCall,
        *,
        response_id: str,
        add_to_conversation: bool,
    ) -> dict[str, Any]:
        pending_item = {
            "id": call.item_id,
            "object": "realtime.item",
            "type": "function_call",
            "status": "in_progress",
            "call_id": call.call_id,
            "name": call.name,
            "arguments": "",
        }
        await self._send_event(
            {
                "type": "response.output_item.added",
                "event_id": f"event_{uuid4().hex}",
                "response_id": response_id,
                "output_index": call.output_index,
                "item": pending_item,
            }
        )
        if add_to_conversation:
            await self._send_event(
                {
                    "type": "conversation.item.added",
                    "event_id": f"event_{uuid4().hex}",
                    "previous_item_id": self._conversation.last_item_id,
                    "item": pending_item,
                }
            )
        await self._send_event(
            {
                "type": "response.function_call_arguments.done",
                "event_id": f"event_{uuid4().hex}",
                "response_id": response_id,
                "item_id": call.item_id,
                "output_index": call.output_index,
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
            }
        )
        completed_item = {
            "id": call.item_id,
            "object": "realtime.item",
            "type": "function_call",
            "status": "completed",
            "call_id": call.call_id,
            "name": call.name,
            "arguments": call.arguments,
        }
        await self._send_event(
            {
                "type": "conversation.item.done",
                "event_id": f"event_{uuid4().hex}",
                "previous_item_id": None,
                "item": completed_item,
            }
        )
        await self._send_event(
            {
                "type": "response.output_item.done",
                "event_id": f"event_{uuid4().hex}",
                "response_id": response_id,
                "output_index": call.output_index,
                "item": completed_item,
            }
        )
        if add_to_conversation:
            self._conversation.upsert_completed(completed_item)
            self._history = self._conversation.responses_input()
        return completed_item

    async def _emit_approval_request(
        self,
        item: dict[str, Any],
        *,
        add_to_conversation: bool,
    ) -> None:
        if not add_to_conversation:
            await self._send_event(
                {
                    "type": "conversation.item.done",
                    "event_id": f"event_{uuid4().hex}",
                    "previous_item_id": None,
                    "item": item,
                }
            )
            return
        async with self._conversation_event_lock:
            await self._send_event(
                {
                    "type": "conversation.item.added",
                    "event_id": f"event_{uuid4().hex}",
                    "previous_item_id": self._conversation.last_item_id,
                    "item": item,
                }
            )
            await self._send_event(
                {
                    "type": "conversation.item.done",
                    "event_id": f"event_{uuid4().hex}",
                    "previous_item_id": None,
                    "item": item,
                }
            )
            self._conversation.upsert_completed(item)
            self._history = self._conversation.responses_input()

    async def _emit_tool_output(
        self,
        call_id: str,
        output: str,
        *,
        add_to_conversation: bool,
    ) -> None:
        item = {
            "id": f"item_{uuid4().hex}",
            "object": "realtime.item",
            "type": "function_call_output",
            "status": "completed",
            "call_id": call_id,
            "output": output,
        }
        if not add_to_conversation:
            return
        async with self._conversation_event_lock:
            await self._send_event(
                {
                    "type": "conversation.item.added",
                    "event_id": f"event_{uuid4().hex}",
                    "previous_item_id": self._conversation.last_item_id,
                    "item": item,
                }
            )
            await self._send_event(
                {
                    "type": "conversation.item.done",
                    "event_id": f"event_{uuid4().hex}",
                    "previous_item_id": None,
                    "item": item,
                }
            )
            self._conversation.upsert_completed(item)
            self._history = self._conversation.responses_input()
