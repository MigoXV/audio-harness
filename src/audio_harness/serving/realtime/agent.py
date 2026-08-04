from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

import numpy as np
import soxr

from audio_harness.capacity import ResizableConcurrencyLimiter
from audio_harness.domain import (
    AsyncResponsesEngine,
    AsyncTtsEngine,
    PresetVoice,
    VoiceSelection,
)
from audio_harness.engines.router import EngineRouter
from audio_harness.pipelines.dag import (
    DuplexTtsNodeConfig,
    McpServerNodeConfig,
    ResponsesAgentNodeConfig,
)
from audio_harness.pipelines.service import ActivePipelineSnapshot
from audio_harness.pipelines.stages import StageWorkflowDefinition
from audio_harness.serving.realtime.tools import (
    McpToolPool,
    ToolRegistry,
    json_tool_output,
)

EventSender = Callable[[dict[str, Any]], Awaitable[None]]
AudioSender = Callable[[bytes, int, int], Awaitable[None]]
AudioClearer = Callable[[int], Awaitable[None]]
AudioFinisher = Callable[[int], Awaitable[None]]
VoiceResolver = Callable[[str], Awaitable[VoiceSelection]]

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


@dataclass(frozen=True)
class _FunctionCall:
    call_id: str
    name: str
    arguments: str
    item_id: str
    output_index: int


class RealtimeAgentRuntime:
    """Responses + 双向流式 TTS 的单会话 Agent 编排器。"""

    def __init__(
        self,
        *,
        pipeline: ActivePipelineSnapshot,
        engine_router: EngineRouter,
        send_event: EventSender,
        send_audio: AudioSender | None,
        clear_audio: AudioClearer | None,
        responses_limiter: ResizableConcurrencyLimiter | None,
        tts_limiter: ResizableConcurrencyLimiter | None,
        tool_registry: ToolRegistry,
        voice_resolver: VoiceResolver | None = None,
        finish_audio: AudioFinisher | None = None,
    ) -> None:
        if isinstance(pipeline.definition, StageWorkflowDefinition):
            response_nodes = [pipeline.plan.nodes.get("responses-agent")]
            tts_nodes = [pipeline.plan.nodes.get("duplex-tts")]
            response_nodes = [node for node in response_nodes if node is not None]
            tts_nodes = [node for node in tts_nodes if node is not None]
        else:
            response_nodes = [
                node
                for node in pipeline.plan.nodes.values()
                if node.node_type.type == "responses_agent"
            ]
            tts_nodes = [
                node
                for node in pipeline.plan.nodes.values()
                if node.node_type.type == "duplex_tts"
            ]
        if len(response_nodes) != 1 or len(tts_nodes) != 1:
            raise RuntimeError("语音 Agent 工作流缺少 Responses 或 TTS 节点")
        response_node = response_nodes[0]
        tts_node = tts_nodes[0]
        if response_node.engine_snapshot is None or tts_node.engine_snapshot is None:
            raise RuntimeError("Agent 节点缺少引擎快照")
        self._pipeline = pipeline
        self._router = engine_router
        self._send_event = send_event
        self._send_audio = send_audio
        self._clear_audio = clear_audio
        self._responses_limiter = responses_limiter
        self._tts_limiter = tts_limiter
        self._tool_registry = tool_registry
        self._voice_resolver = voice_resolver
        self._finish_audio = finish_audio
        self._response_snapshot = response_node.engine_snapshot
        self._tts_snapshot = tts_node.engine_snapshot
        self._response_config = cast(
            ResponsesAgentNodeConfig,
            response_node.config,
        )
        self._tts_config = cast(DuplexTtsNodeConfig, tts_node.config)
        self.options = AgentSessionOptions(
            voice=self._tts_config.default_voice,
        )
        self._history: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None
        self._epoch = 0
        self._active_response_id: str | None = None
        self._pending_client_tools: dict[str, asyncio.Future[str]] = {}
        self._pending_approvals: dict[str, asyncio.Future[bool]] = {}
        self._closed = False
        self._voice_locked = False

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def session_document(self, *, model: str) -> dict[str, Any]:
        return {
            "id": f"sess_{uuid4().hex}",
            "object": "realtime.session",
            "type": "realtime",
            "model": model,
            "instructions": self.options.instructions,
            "output_modalities": self.options.output_modalities,
            "max_output_tokens": self._response_config.max_output_tokens,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24_000},
                    "transcription": {"model": model},
                    "turn_detection": {"type": "server_vad"},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24_000},
                    "voice": self.options.voice,
                },
            },
            "tools": self.options.tools,
            "tool_choice": self.options.tool_choice,
            "truncation": self.options.truncation,
        }

    def update_options(self, payload: dict[str, Any]) -> None:
        if payload.get("type", "realtime") != "realtime":
            raise ValueError("语音 Agent 模型只支持 realtime session")
        modalities = payload.get("output_modalities")
        if modalities is not None:
            if modalities not in (["audio"], ["text"]):
                raise ValueError("output_modalities 只支持 ['audio'] 或 ['text']")
            self.options.output_modalities = list(modalities)
        if payload.get("instructions") is not None:
            self.options.instructions = str(payload["instructions"])
        audio_output = ((payload.get("audio") or {}).get("output") or {})
        if audio_output.get("voice") is not None:
            voice = str(audio_output["voice"])
            if self._voice_locked and voice != self.options.voice:
                raise ValueError("会话已经输出音频，不能再修改音色")
            if voice not in self._tts_config.allowed_voices:
                raise ValueError(f"工作流不允许音色: {voice}")
            self.options.voice = voice
        tools = payload.get("tools")
        if tools is not None:
            values = []
            for tool in tools:
                if tool.get("type") not in {"function", "mcp"}:
                    raise ValueError("客户端工具只支持 function 或 mcp")
                values.append(dict(tool))
            self.options.tools = values
        if payload.get("tool_choice") is not None:
            self.options.tool_choice = payload["tool_choice"]
        truncation = payload.get("truncation")
        if truncation is not None:
            if truncation not in {"auto", "disabled"}:
                raise ValueError("truncation 只支持 auto 或 disabled")
            self.options.truncation = truncation

    async def respond(self, transcript: str, *, input_item_id: str) -> None:
        if self._closed:
            return
        await self.interrupt(reason="superseded", emit=False)
        self._history.append(
            {
                "role": "user",
                "content": [{"type": "input_text", "text": transcript}],
            }
        )
        self._truncate_history()
        epoch = self._epoch
        self._task = asyncio.create_task(
            self._run_response(epoch, input_item_id),
            name=f"realtime-agent-response-{epoch}",
        )

    async def interrupt(
        self,
        *,
        reason: str = "interrupted",
        emit: bool = True,
    ) -> None:
        task = self._task
        response_id = self._active_response_id
        self._epoch += 1
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if emit and response_id is not None:
                await self._send_event(
                    {
                        "type": "response.done",
                        "event_id": f"event_{uuid4().hex}",
                        "response": {
                            "id": response_id,
                            "object": "realtime.response",
                            "status": "cancelled",
                            "status_details": {
                                "type": "cancelled",
                                "reason": reason,
                            },
                            "output": [],
                        },
                    }
                )
        self._task = None
        self._active_response_id = None
        if self._clear_audio is not None:
            await self._clear_audio(self._epoch)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.interrupt(emit=False)

    async def submit_function_output(self, call_id: str, output: str) -> None:
        future = self._pending_client_tools.pop(call_id, None)
        if future is None:
            raise ValueError(f"不存在待处理的 function call: {call_id}")
        self._history.append(
            {"type": "function_call_output", "call_id": call_id, "output": output}
        )
        if not future.done():
            future.set_result(output)

    async def submit_mcp_approval(self, request_id: str, approve: bool) -> None:
        future = self._pending_approvals.pop(request_id, None)
        if future is None:
            raise ValueError(f"不存在待审批的 MCP 调用: {request_id}")
        if not future.done():
            future.set_result(approve)

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

    async def _run_response(self, epoch: int, input_item_id: str) -> None:
        response_id = f"resp_{uuid4().hex}"
        output_item_id = f"item_{uuid4().hex}"
        self._active_response_id = response_id
        await self._send_event(
            {
                "type": "response.created",
                "event_id": f"event_{uuid4().hex}",
                "response": {
                    "id": response_id,
                    "object": "realtime.response",
                    "status": "in_progress",
                    "output": [],
                    "output_modalities": self.options.output_modalities,
                },
            }
        )
        try:
            response_slot = (
                self._responses_limiter.try_slot()
                if self._responses_limiter is not None
                else _unlimited_slot()
            )
            audio_output = self.options.output_modalities == ["audio"]
            tts_slot = (
                self._tts_limiter.try_slot()
                if audio_output and self._tts_limiter is not None
                else _unlimited_slot()
            )
            async with response_slot, tts_slot:
                async with self._router.lease_snapshots(
                    (self._response_snapshot, self._tts_snapshot)
                    if audio_output
                    else (self._response_snapshot,)
                ) as leased:
                    responses = cast(
                        AsyncResponsesEngine,
                        leased[
                            (
                                self._response_snapshot.id,
                                self._response_snapshot.revision,
                            )
                        ],
                    )
                    tts = (
                        cast(
                            AsyncTtsEngine,
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
                        responses=responses,
                        tts=tts,
                    )
            if epoch != self._epoch:
                return
            await self._send_event(
                {
                    "type": "response.done",
                    "event_id": f"event_{uuid4().hex}",
                    "response": {
                        "id": response_id,
                        "object": "realtime.response",
                        "status": "completed",
                        "output": [completed_item],
                        "output_modalities": self.options.output_modalities,
                    },
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
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
                    "response": {
                        "id": response_id,
                        "object": "realtime.response",
                        "status": "failed",
                        "output": [],
                    },
                }
            )
        finally:
            if self._active_response_id == response_id:
                self._active_response_id = None

    async def _stream_turn(
        self,
        *,
        epoch: int,
        response_id: str,
        output_item_id: str,
        responses: AsyncResponsesEngine,
        tts: AsyncTtsEngine | None,
    ) -> dict[str, Any] | None:
        mcp_configs = list(self._response_config.mcp_servers)
        memory_headers: dict[str, dict[str, str]] = {}
        for tool in self.options.tools:
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
                McpServerNodeConfig(
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
        for tool in self.options.tools:
            if tool.get("type") == "function":
                tool_definitions.append(tool)
                if tool.get("name"):
                    client_tool_names.add(str(tool["name"]))
        instructions = self._response_config.instructions
        if self.options.instructions:
            instructions += "\n\n" + self.options.instructions
        tool_choice, constraint = self._tool_choice()
        if constraint:
            instructions += "\n\n" + constraint

        # Responses 的输出受 max_output_tokens 限制。使用无界队列可避免
        # TTS 提前失败后生产者卡在已经无人消费的有界队列上。
        text_queue: asyncio.Queue[str | object] = asyncio.Queue()
        audio_task = (
            asyncio.create_task(
                self._stream_tts(
                    epoch=epoch,
                    response_id=response_id,
                    output_item_id=output_item_id,
                    tts=tts,
                    text_queue=text_queue,
                )
            )
            if tts is not None
            else None
        )
        full_text: list[str] = []
        next_function_output_index = 1
        await self._emit_output_started(output_item_id, audio=tts is not None)
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
        if epoch != self._epoch:
            return
        text = "".join(full_text)
        if text:
            self._history.append(
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            )
        return await self._emit_output_done(
            output_item_id,
            text=text,
            audio=tts is not None,
        )

    def _tool_choice(self) -> tuple[Any, str | None]:
        choice = self.options.tool_choice
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
        responses: AsyncResponsesEngine,
        instructions: str,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        response_id: str,
        output_item_id: str,
        text_queue: asyncio.Queue[str | object],
        full_text: list[str],
        audio: bool,
        function_output_index: int,
    ) -> list[_FunctionCall]:
        kwargs: dict[str, Any] = {
            "model": self._response_config.model,
            "instructions": instructions,
            "input": list(self._history),
            "max_output_tokens": self._response_config.max_output_tokens,
            "reasoning": {
                "effort": (
                    "high"
                    if self._response_config.thinking_enabled
                    else "none"
                )
            },
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
            kwargs["parallel_tool_calls"] = True
        stream = await responses.stream_response(**kwargs)
        calls: list[_FunctionCall] = []
        try:
            async for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    delta = event.delta
                    full_text.append(delta)
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
                        self._history.append(
                            item.model_dump(mode="json", exclude_none=True)
                            if hasattr(item, "model_dump")
                            else {
                                "type": "function_call",
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments": call.arguments,
                            }
                        )
                        await self._emit_function_call(call)
                    elif item_type == "reasoning" and hasattr(
                        item,
                        "model_dump",
                    ):
                        self._history.append(
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
                        self._pending_client_tools[call.call_id] = future
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(future),
                                timeout=self._response_config.tool_timeout_seconds,
                            )
                        except asyncio.TimeoutError:
                            self._pending_client_tools.pop(call.call_id, None)
                            raise
                        return
                    mcp_tool = mcp.tool(call.name)
                    if mcp_tool is not None:
                        if mcp_tool.approval_required():
                            request_id = f"approval_{uuid4().hex}"
                            future = asyncio.get_running_loop().create_future()
                            self._pending_approvals[request_id] = future
                            await self._send_event(
                                {
                                    "type": "conversation.item.created",
                                    "event_id": f"event_{uuid4().hex}",
                                    "previous_item_id": None,
                                    "item": {
                                        "id": request_id,
                                        "type": "mcp_approval_request",
                                        "server_label": mcp_tool.server_label,
                                        "name": mcp_tool.remote_name,
                                        "arguments": call.arguments,
                                    },
                                }
                            )
                            approved = await asyncio.wait_for(
                                asyncio.shield(future),
                                timeout=self._response_config.tool_timeout_seconds,
                            )
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
                    self._history.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": output,
                        }
                    )
                    await self._emit_tool_output(call.call_id, output)
                except asyncio.CancelledError:
                    raise
        await asyncio.gather(*(execute(call) for call in calls))

    async def _stream_tts(
        self,
        *,
        epoch: int,
        response_id: str,
        output_item_id: str,
        tts: AsyncTtsEngine,
        text_queue: asyncio.Queue[str | object],
    ) -> None:
        async def chunks() -> AsyncIterator[str]:
            while True:
                item = await text_queue.get()
                if item is _TEXT_END:
                    return
                assert isinstance(item, str)
                yield item

        voice = self.options.voice or self._tts_config.default_voice
        selection = (
            await self._voice_resolver(voice)
            if self._voice_resolver is not None
            else PresetVoice(voice)
        )
        wss_sample_rate: int | None = None
        wss_resampler: soxr.ResampleStream | None = None
        first_audio_sent = False

        async def emit_first_audio() -> None:
            nonlocal first_audio_sent
            if first_audio_sent:
                return
            first_audio_sent = True
            await self._send_event(
                {
                    "type": "audio_harness.tts_first_audio",
                    "event_id": f"event_{uuid4().hex}",
                    "response_id": response_id,
                    "item_id": output_item_id,
                    "output_index": 0,
                    "content_index": 0,
                }
            )

        async def send_wss_audio(pcm: bytes) -> None:
            if not pcm:
                return
            await emit_first_audio()
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
            if epoch != self._epoch:
                return
            if self._send_audio is not None:
                await emit_first_audio()
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
        if wss_resampler is not None and epoch == self._epoch:
            tail = wss_resampler.resample_chunk(
                np.empty(0, dtype=np.int16),
                last=True,
            ).astype("<i2", copy=False).tobytes()
            await send_wss_audio(tail)
        if epoch == self._epoch and self._finish_audio is not None:
            await self._finish_audio(epoch)

    async def _emit_output_started(self, item_id: str, *, audio: bool) -> None:
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
                "response_id": self._active_response_id,
                "output_index": 0,
                "item": item,
            }
        )
        await self._send_event(
            {
                "type": "response.content_part.added",
                "event_id": f"event_{uuid4().hex}",
                "response_id": self._active_response_id,
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
        item_id: str,
        *,
        text: str,
        audio: bool,
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
                "response_id": self._active_response_id,
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
                    "response_id": self._active_response_id,
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
            "status": "completed",
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
                "response_id": self._active_response_id,
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "part": part,
            }
        )
        await self._send_event(
            {
                "type": "response.output_item.done",
                "event_id": f"event_{uuid4().hex}",
                "response_id": self._active_response_id,
                "output_index": 0,
                "item": item,
            }
        )
        return item

    async def _emit_function_call(self, call: _FunctionCall) -> None:
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
                "response_id": self._active_response_id,
                "output_index": call.output_index,
                "item": pending_item,
            }
        )
        await self._send_event(
            {
                "type": "response.function_call_arguments.done",
                "event_id": f"event_{uuid4().hex}",
                "response_id": self._active_response_id,
                "item_id": call.item_id,
                "output_index": call.output_index,
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
            }
        )
        await self._send_event(
            {
                "type": "response.output_item.done",
                "event_id": f"event_{uuid4().hex}",
                "response_id": self._active_response_id,
                "output_index": call.output_index,
                "item": {
                    "id": call.item_id,
                    "object": "realtime.item",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                },
            }
        )

    async def _emit_tool_output(self, call_id: str, output: str) -> None:
        await self._send_event(
            {
                "type": "conversation.item.created",
                "event_id": f"event_{uuid4().hex}",
                "previous_item_id": None,
                "item": {
                    "id": f"item_{uuid4().hex}",
                    "object": "realtime.item",
                    "type": "function_call_output",
                    "status": "completed",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )
