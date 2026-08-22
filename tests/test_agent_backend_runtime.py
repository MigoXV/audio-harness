from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from types import SimpleNamespace
from typing import Any

import pytest

from audio_harness.agent_backends.base import (
    AgentBackendGateway,
    AgentBackendSession,
)
from audio_harness.agent_backends.models import (
    AgentBackendCapabilities,
    AgentBackendEvent,
    AgentBackendEventKind,
    AgentBackendKind,
    AgentBackendProtocol,
    AgentBackendSnapshot,
)
from audio_harness.serving.realtime.agent import RealtimeAgentRuntime
from audio_harness.serving.realtime.backend import RealtimeAgentBackendRuntime
from audio_harness.serving.realtime.tools import ToolRegistry
from audio_harness.voice_agents.models import (
    AgentBackendBinding,
    VoiceAgentDefinition,
)
from tests.test_agent_runtime import FakeTts, HangingTts, _profile


_SESSION_CLOSED = object()


class FakeBackendSession(AgentBackendSession):
    def __init__(self) -> None:
        self._session_id = "dsh-session-1"
        self.queue: asyncio.Queue[AgentBackendEvent | object] = asyncio.Queue()
        self.prompts: list[tuple[str, str]] = []
        self.cancel_calls = 0
        self.approvals: list[tuple[str, str, bool]] = []
        self.answers: list[tuple[str, Any]] = []

    @property
    def session_id(self) -> str:
        return self._session_id

    async def prompt(
        self,
        text: str,
        *,
        mode: str = "queue",
        client_time_zone: str | None = None,
    ) -> None:
        self.prompts.append((mode, text))

    async def cancel(self) -> None:
        self.cancel_calls += 1

    async def respond_approval(
        self,
        request_id: str,
        approval_id: str,
        *,
        approved: bool,
    ) -> None:
        self.approvals.append((request_id, approval_id, approved))

    async def respond_question(self, request_id: str, answers) -> None:
        self.answers.append((request_id, answers))

    async def _events(self):
        while True:
            event = await self.queue.get()
            if event is _SESSION_CLOSED:
                return
            assert isinstance(event, AgentBackendEvent)
            yield event

    def events(self):
        return self._events()

    async def push(
        self,
        kind: AgentBackendEventKind,
        *,
        seq: int | None = None,
        request_id: str | None = None,
        **data: Any,
    ) -> None:
        await self.queue.put(
            AgentBackendEvent(
                kind=kind,
                session_id=self.session_id,
                seq=seq,
                request_id=request_id,
                data=data,
            )
        )

    async def close(self) -> None:
        await self.queue.put(_SESSION_CLOSED)


class FakeBackendGateway(AgentBackendGateway):
    def __init__(self) -> None:
        self.session = FakeBackendSession()
        self.opens: list[tuple[AgentBackendSnapshot, str, str]] = []

    async def capabilities(self, snapshot):
        return AgentBackendCapabilities(
            wire_version="0.0.1",
            provider="test",
            model="test",
            cwd="/workspace",
            presets=("standard",),
            plugins=(),
        )

    async def open_session(self, snapshot, *, cwd, agent_preset):
        self.opens.append((snapshot, cwd, agent_preset))
        return self.session

    async def retire(self, backend_id, *, before_revision=None):
        return None

    async def close(self):
        await self.session.close()


class ScriptedResponses:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def stream_response(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("unexpected Responses call")
        output = self.outputs.pop(0)

        async def events():
            yield SimpleNamespace(
                type="response.output_text.delta",
                delta=output,
            )

        return events()


class BackendPool:
    def __init__(self, outputs: list[str], *, tts=None) -> None:
        self.responses = ScriptedResponses(outputs)
        self.tts = tts or FakeTts()

    @asynccontextmanager
    async def lease(self, _snapshots):
        yield {
            ("responses-engine", 1): self.responses,
            ("tts-engine", 1): self.tts,
        }


def decision(
    action: str,
    *,
    message: str | None = None,
    task: str | None = None,
    approval: bool | None = None,
    answers: list[dict[str, Any]] | None = None,
) -> str:
    return json.dumps(
        {
            "action": action,
            "message": message,
            "task": task,
            "approval": approval,
            "answers": answers,
        },
        ensure_ascii=False,
    )


def backend_profile(*, task_timeout: float = 900, interaction_timeout: float = 120):
    base = _profile()
    payload = base.definition.model_dump(mode="json")
    payload.update(
        {
            "schema_version": 2,
            "agent_backend": {
                "backend_id": "backend-1",
                "cwd": "/workspace/project",
                "agent_preset": "standard",
                "coordinator_instructions": "优先委派文件操作",
                "task_timeout_seconds": task_timeout,
                "interaction_timeout_seconds": interaction_timeout,
            },
        }
    )
    snapshot = AgentBackendSnapshot(
        id="backend-1",
        revision=4,
        name="DSH",
        kind=AgentBackendKind.DSH,
        protocol=AgentBackendProtocol.DSH_WEB_V1,
        target="http://dsh.test",
        connect_timeout_seconds=1,
        request_timeout_seconds=10,
    )
    return replace(
        base,
        definition=VoiceAgentDefinition.model_validate(payload),
        agent_backend=snapshot,
    )


async def wait_until(predicate, timeout: float = 2) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


def runtime_for(pool, gateway, events, *, extension_events_enabled: bool = True):
    async def send_event(event: dict[str, Any]) -> None:
        events.append(event)

    return RealtimeAgentRuntime(
        profile=backend_profile(),
        inferencer_pool=pool,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
        agent_backend_gateway=gateway,
        backend_extension_events_enabled=extension_events_enabled,
    )


@pytest.mark.asyncio
async def test_delegate_then_bounded_no_tool_summary() -> None:
    pool = BackendPool(
        [
            decision(
                "delegate",
                message="我去检查，稍后汇报。",
                task="检查磁盘与显存",
            ),
            "磁盘已用九成；GPU 显存查询受容器限制。",
        ]
    )
    gateway = FakeBackendGateway()
    events: list[dict[str, Any]] = []
    runtime = runtime_for(pool, gateway, events)
    try:
        await runtime.respond("查一下磁盘和显存", input_item_id="user-1")
        await wait_until(lambda: gateway.session.prompts == [("queue", "检查磁盘与显存")])
        await wait_until(lambda: not runtime.active)
        coordinator_call = pool.responses.calls[0]
        assert coordinator_call["max_output_tokens"] == 512
        assert coordinator_call["text"]["format"]["strict"] is True
        assert "tools" not in coordinator_call

        await gateway.session.push(
            AgentBackendEventKind.TURN_STARTED,
            seq=1,
            turn=1,
        )
        await gateway.session.push(
            AgentBackendEventKind.TOOL_STARTED,
            seq=2,
            call_id="call-1",
            name="bash",
            arguments="x" * 10_000,
        )
        await gateway.session.push(
            AgentBackendEventKind.TOOL_COMPLETED,
            seq=3,
            call_id="call-1",
            message="y" * 20_000,
            error=None,
        )
        await gateway.session.push(
            AgentBackendEventKind.MESSAGE_COMPLETED,
            seq=4,
            text="磁盘 90%，NVML 不可用",
        )
        await gateway.session.push(
            AgentBackendEventKind.TURN_COMPLETED,
            seq=5,
            turn=1,
            reason={"kind": "completed"},
        )
        await wait_until(lambda: len(pool.responses.calls) == 2)
        await wait_until(lambda: not runtime.active)
        summary_call = pool.responses.calls[1]
        assert "tools" not in summary_call
        summary_input = json.dumps(summary_call["input"], ensure_ascii=False)
        assert "磁盘 90%" in summary_input
        assert len(summary_input) < 20_000
        tool_started = next(
            event
            for event in events
            if event["type"] == "x_aha.agent_backend.tool.started"
        )
        tool_completed = next(
            event
            for event in events
            if event["type"] == "x_aha.agent_backend.tool.completed"
        )
        assert len(tool_started["arguments"]) == 4_097
        assert len(tool_completed["result"]) == 8_193
        assert pool.tts.text == [
            "我去检查，稍后汇报。",
            "磁盘已用九成；GPU 显存查询受容器限制。",
        ]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_reply_keeps_responses_tools_and_does_not_open_backend() -> None:
    pool = BackendPool([decision("reply"), "直接回答"])
    gateway = FakeBackendGateway()
    events: list[dict[str, Any]] = []
    registry = ToolRegistry()
    registry.register(
        name="local_status",
        description="status",
        parameters={"type": "object", "properties": {}},
        handler=lambda _args: "ok",
    )

    async def send_event(event: dict[str, Any]) -> None:
        events.append(event)

    profile = backend_profile()
    profile.definition.responses.service_tools.append("local_status")
    runtime = RealtimeAgentRuntime(
        profile=profile,
        inferencer_pool=pool,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=registry,
        agent_backend_gateway=gateway,
        backend_extension_events_enabled=True,
    )
    try:
        runtime.update_options(
            {
                "tools": [
                    {
                        "type": "function",
                        "name": "client_status",
                        "description": "client",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ]
            }
        )
        await runtime.respond("你好", input_item_id="user-1")
        await wait_until(lambda: not runtime.active)
        assert gateway.opens == []
        assert "tools" not in pool.responses.calls[0]
        direct_instructions = pool.responses.calls[1]["instructions"]
        assert '"name":"DSH"' in direct_instructions
        assert "后台会话尚未建立" in direct_instructions
        assert "http://dsh.test" not in direct_instructions
        assert "/workspace/project" not in direct_instructions
        names = {tool["name"] for tool in pool.responses.calls[1]["tools"]}
        assert names == {"local_status", "client_status"}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_invalid_decision_repairs_once_without_calling_backend() -> None:
    pool = BackendPool(["not json", "still not json"])
    gateway = FakeBackendGateway()
    runtime = runtime_for(pool, gateway, [])
    try:
        await runtime.respond("执行危险操作", input_item_id="user-1")
        await wait_until(lambda: not runtime.active)
        assert len(pool.responses.calls) == 2
        assert gateway.opens == []
        assert "没有执行后台操作" in "".join(pool.tts.text)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_response_cancel_does_not_cancel_backend_but_disconnect_does() -> None:
    tts = HangingTts()
    pool = BackendPool(
        [decision("delegate", task="长任务", message="开始处理")],
        tts=tts,
    )
    gateway = FakeBackendGateway()
    runtime = runtime_for(pool, gateway, [])
    await runtime.respond("做个长任务", input_item_id="user-1")
    await wait_until(lambda: bool(gateway.session.prompts))
    await asyncio.wait_for(tts.started.wait(), 1)
    response_id = runtime.active_response_id
    assert response_id is not None
    await runtime.interrupt(
        reason="client_cancelled",
        response_id=response_id,
    )
    assert gateway.session.cancel_calls == 0
    await runtime.close()
    assert gateway.session.cancel_calls == 1


@pytest.mark.asyncio
async def test_running_task_uses_steer_and_voice_approval_is_first_wins() -> None:
    pool = BackendPool(
        [
            decision("delegate", task="初始任务"),
            decision("steer", task="改成只读检查"),
            decision("approve", approval=True),
            decision(
                "answer",
                answers=[{"id": "q1", "selected": ["A"], "custom": None}],
            ),
        ]
    )
    gateway = FakeBackendGateway()
    events: list[dict[str, Any]] = []
    runtime = runtime_for(pool, gateway, events)
    try:
        await runtime.respond("开始", input_item_id="user-1")
        await wait_until(lambda: len(gateway.session.prompts) == 1)
        await wait_until(lambda: not runtime.active)
        await runtime.respond("改成只读", input_item_id="user-2")
        await wait_until(lambda: len(gateway.session.prompts) == 2)
        assert gateway.session.prompts == [
            ("queue", "初始任务"),
            ("steer", "改成只读检查"),
        ]
        await wait_until(lambda: not runtime.active)
        await gateway.session.push(
            AgentBackendEventKind.APPROVAL_REQUESTED,
            request_id="approval-rpc",
            approval_id="approval-1",
            tool_name="bash",
            call_id="call-1",
            reason="需要执行命令",
        )
        await wait_until(lambda: runtime._backend.latest_pending() is not None)
        await runtime.respond("允许", input_item_id="user-3")
        await wait_until(lambda: len(gateway.session.approvals) == 1)
        assert gateway.session.approvals == [
            ("approval-rpc", "approval-1", True)
        ]
        assert (
            await runtime.submit_backend_approval(
                "approval-rpc",
                "approval-1",
                approved=False,
            )
            is False
        )
        await wait_until(
            lambda: any(
                event.get("resolution") == "already_processed"
                for event in events
            )
        )
        await wait_until(lambda: not runtime.active)
        await gateway.session.push(
            AgentBackendEventKind.QUESTION_REQUESTED,
            request_id="question-rpc",
            questions=[
                {
                    "id": "q1",
                    "question": "选择哪个？",
                    "options": [{"label": "A"}],
                    "multiSelect": False,
                }
            ],
        )
        await wait_until(
            lambda: runtime._backend.latest_pending() is not None
            and runtime._backend.latest_pending().kind == "question"
        )
        await runtime.respond("选 A", input_item_id="user-4")
        await wait_until(lambda: len(gateway.session.answers) == 1)
        assert gateway.session.answers == [
            (
                "question-rpc",
                [{"id": "q1", "selected": ["A"]}],
            )
        ]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_interaction_timeouts_default_to_reject_and_cancel() -> None:
    gateway = FakeBackendGateway()
    events: list[dict[str, Any]] = []
    notices: list[str] = []

    async def send_event(event: dict[str, Any]) -> None:
        events.append(event)

    async def notice(text: str) -> None:
        notices.append(text)

    async def summarize(_result) -> None:
        return None

    backend = RealtimeAgentBackendRuntime(
        snapshot=backend_profile().agent_backend,
        binding=AgentBackendBinding(
            backend_id="backend-1",
            cwd="/workspace",
            interaction_timeout_seconds=0.02,
        ),
        gateway=gateway,
        send_event=send_event,
        speak_notice=notice,
        summarize=summarize,
        extension_events_enabled=True,
    )
    try:
        await backend.ensure_session()
        await gateway.session.push(
            AgentBackendEventKind.APPROVAL_REQUESTED,
            request_id="approval-timeout",
            approval_id="approval-1",
            tool_name="bash",
        )
        await wait_until(lambda: len(gateway.session.approvals) == 1)
        assert gateway.session.approvals == [
            ("approval-timeout", "approval-1", False)
        ]
        await gateway.session.push(
            AgentBackendEventKind.QUESTION_REQUESTED,
            request_id="question-timeout",
            questions=[{"id": "q", "question": "Q?", "options": []}],
        )
        await wait_until(lambda: len(gateway.session.answers) == 1)
        assert gateway.session.answers == [("question-timeout", None)]
        timed_out = [
            event
            for event in events
            if event.get("resolution") == "timed_out"
        ]
        assert {event["type"] for event in timed_out} == {
            "x_aha.agent_backend.approval.resolved",
            "x_aha.agent_backend.question.resolved",
        }
        assert len(notices) == 2
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_native_client_receives_no_agent_backend_extension_events() -> None:
    pool = BackendPool([decision("delegate", task="检查")])
    gateway = FakeBackendGateway()
    events: list[dict[str, Any]] = []
    runtime = runtime_for(
        pool,
        gateway,
        events,
        extension_events_enabled=False,
    )
    try:
        await runtime.respond("检查", input_item_id="user-1")
        await wait_until(lambda: bool(gateway.session.prompts))
        await gateway.session.push(
            AgentBackendEventKind.TURN_STARTED,
            seq=1,
            turn=1,
        )
        await wait_until(lambda: not runtime.active)
        assert all(not event["type"].startswith("x_aha.") for event in events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_backend_task_timeout_cancels_dsh_run() -> None:
    gateway = FakeBackendGateway()
    events: list[dict[str, Any]] = []

    async def send_event(event: dict[str, Any]) -> None:
        events.append(event)

    async def no_text(_value) -> None:
        return None

    backend = RealtimeAgentBackendRuntime(
        snapshot=backend_profile().agent_backend,
        binding=AgentBackendBinding(
            backend_id="backend-1",
            cwd="/workspace",
            task_timeout_seconds=0.02,
        ),
        gateway=gateway,
        send_event=send_event,
        speak_notice=no_text,
        summarize=no_text,
        extension_events_enabled=True,
    )
    try:
        await backend.delegate("long", mode="queue")
        await wait_until(lambda: gateway.session.cancel_calls == 1)
        assert backend.running is False
        assert any(
            event["type"] == "x_aha.agent_backend.failed"
            and event["code"] == "task_timeout"
            for event in events
        )
    finally:
        await backend.close()
