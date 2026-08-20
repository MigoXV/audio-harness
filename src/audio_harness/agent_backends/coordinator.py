from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from audio_harness.inferencers.responses.http import ResponsesInferencer


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentBackendQuestionAnswer(_StrictModel):
    id: str = Field(min_length=1, max_length=200)
    selected: list[str] = Field(max_length=50)
    custom: str | None = Field(max_length=4_000)


class AgentBackendDecision(_StrictModel):
    action: Literal["reply", "delegate", "steer", "cancel", "approve", "answer"]
    message: str | None = Field(max_length=4_000)
    task: str | None = Field(max_length=16_000)
    approval: bool | None
    answers: list[AgentBackendQuestionAnswer] | None = Field(max_length=20)


class AgentBackendDecisionError(RuntimeError):
    pass


_BASE_INSTRUCTIONS = """
你是语音智能体与外部 Agent Backend 之间的协调器。你只能输出给定 JSON Schema。

动作语义：
- reply：由当前 Responses 直接回答，不调用 backend。普通问答、进度查询、以及可由当前 Responses 工具完成的请求使用它。
- delegate：把新的、需要操作远端工作区或执行较长任务的请求交给 backend；task 写完整可执行任务。
- steer：backend 正在运行时，用户补充或修正当前任务；task 写修正指令。
- cancel：用户明确要求取消 backend 任务。
- approve：存在待审批请求，用户在回答是否允许；approval 必须为 true 或 false。
- answer：存在待回答的结构化问题；answers 必须完整匹配问题 id。

不要把 Agent Backend 当作工具，不要声称已完成尚未执行的任务。message 只用于委派、修正、取消、审批或回答后的简短口语确认；reply 时可为 null。没有适用值的字段必须为 null。
""".strip()


async def _collect_text(stream: Any) -> str:
    chunks: list[str] = []
    try:
        async for event in stream:
            event_type = getattr(event, "type", "")
            if event_type == "response.output_text.delta":
                chunks.append(str(getattr(event, "delta", "")))
            elif event_type in {"response.failed", "response.incomplete", "error"}:
                detail = getattr(event, "error", None) or getattr(
                    getattr(event, "response", None),
                    "error",
                    None,
                )
                message = getattr(detail, "message", None) or str(detail)
                raise AgentBackendDecisionError(
                    f"Responses 协调调用失败: {message}"
                )
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            await close()
    return "".join(chunks)


async def decide_agent_backend_action(
    responses: ResponsesInferencer,
    *,
    model: str,
    history: list[dict[str, Any]],
    backend_context: Mapping[str, Any],
    coordinator_instructions: str = "",
    reasoning_effort: str = "none",
) -> AgentBackendDecision:
    instructions = _BASE_INSTRUCTIONS
    if coordinator_instructions.strip():
        instructions += "\n\n[部署方协调指令]\n" + coordinator_instructions.strip()
    instructions += (
        "\n\n[当前 Backend 状态]\n"
        + json.dumps(backend_context, ensure_ascii=False, separators=(",", ":"))
    )
    schema = AgentBackendDecision.model_json_schema()
    last_error: Exception | None = None
    for attempt in range(2):
        attempt_instructions = instructions
        if attempt:
            attempt_instructions += (
                "\n\n上一轮输出无法按 schema 解析。请只重新输出一个严格合法的 JSON 对象。"
            )
        try:
            stream = await responses.stream_response(
                model=model,
                instructions=attempt_instructions,
                input=list(history),
                max_output_tokens=512,
                reasoning={"effort": reasoning_effort},
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "agent_backend_decision",
                        "strict": True,
                        "schema": schema,
                    }
                },
            )
            raw = await _collect_text(stream)
            return AgentBackendDecision.model_validate_json(raw)
        except (AgentBackendDecisionError, ValidationError, ValueError) as exc:
            last_error = exc
    raise AgentBackendDecisionError(
        f"Responses 协调输出连续两次无法解析: {last_error}"
    )
