from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import replace
import json
from typing import Any

import httpx
import pytest

from audio_harness.agent_backends.dsh import (
    AgentBackendResponseRejected,
    DshProtocolError,
    DshRpcError,
    DshWebGateway,
)
from audio_harness.agent_backends.models import (
    AgentBackendEventKind,
    AgentBackendKind,
    AgentBackendProtocol,
    AgentBackendSnapshot,
)


_SOCKET_CLOSED = object()


class FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | object] = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        value = await self.incoming.get()
        if value is _SOCKET_CLOSED:
            raise StopAsyncIteration
        assert isinstance(value, str)
        return value

    async def close_from_server(self) -> None:
        await self.incoming.put(_SOCKET_CLOSED)


class FakeWebSocketContext:
    def __init__(self, socket: FakeWebSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> FakeWebSocket:
        return self.socket

    async def __aexit__(self, *_args) -> None:
        return None


class FakeWebSocketHub:
    def __init__(self) -> None:
        self.sockets: dict[str, list[FakeWebSocket]] = defaultdict(list)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def connect(self, url: str, **kwargs: Any) -> FakeWebSocketContext:
        stream = url.rsplit("/", 1)[-1]
        socket = FakeWebSocket()
        self.sockets[stream].append(socket)
        self.calls.append((stream, kwargs))
        return FakeWebSocketContext(socket)

    async def wait_connected(self, stream: str, count: int = 1) -> None:
        async def wait() -> None:
            while len(self.sockets[stream]) < count:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), timeout=2)

    async def push(
        self,
        stream: str,
        payload: dict[str, Any],
        *,
        rpc_id: str = "push-1",
        connection: int = -1,
    ) -> None:
        await self.wait_connected(stream, abs(connection) if connection < 0 else connection + 1)
        await self.sockets[stream][connection].incoming.put(
            json.dumps(
                {
                    "type": "server-request",
                    "rpcId": rpc_id,
                    "method": payload["type"],
                    "payload": payload,
                }
            )
        )


class FakeDshHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], httpx.Headers]] = []
        self.history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.override: dict[str, Any] = {}
        self.receipt: dict[str, Any] = {"accepted": True}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = request.url.path.removeprefix("/root/api/").removeprefix("/api/")
        self.calls.append((method, body, request.headers))
        if method == "respond":
            return httpx.Response(200, json=self.receipt)
        if method in self.override:
            override = self.override[method]
            if isinstance(override, Exception):
                raise override
            return httpx.Response(200, json=override)
        value: Any
        if method == "host.describe":
            value = {
                "version": "0.0.1",
                "cwd": "/srv/dsh",
                "provider": "deepseek-official",
                "model": "deepseek-v4-flash",
            }
        elif method == "agentPreset.list":
            value = {
                "presets": [
                    {"id": "standard", "trust": "workspace-write", "isDefault": True},
                    {"id": "minimal", "trust": "read-only", "isDefault": False},
                ]
            }
        elif method == "pluginInventory/list":
            value = {
                "entries": [
                    {
                        "entryId": "tool-bash",
                        "moduleName": "@deepseek-ai/dsh-tool-bash",
                        "enabled": True,
                        "fiberPhase": "active",
                    }
                ]
            }
        elif method == "session.create":
            value = {
                "sessionId": body["payload"]["sessionId"],
                "agentPreset": body["payload"]["agentPreset"],
            }
        elif method == "session.prompt":
            value = {"accepted": True}
        elif method == "session.cancel":
            value = {"cancelled": True}
        elif method == "session.history":
            value = {
                "events": self.history[body["payload"]["sessionId"]],
                "hasMore": False,
            }
        else:
            raise AssertionError(f"unexpected DSH method: {method}")
        return httpx.Response(
            200,
            json={
                "type": "server-response",
                "rpcId": body["rpcId"],
                "result": {"ok": True, "value": value},
            },
        )


def snapshot(*, target: str = "http://dsh.test/root") -> AgentBackendSnapshot:
    return AgentBackendSnapshot(
        id="backend-1",
        revision=3,
        name="DSH",
        kind=AgentBackendKind.DSH,
        protocol=AgentBackendProtocol.DSH_WEB_V1,
        target=target,
        connect_timeout_seconds=1,
        request_timeout_seconds=2,
        headers_env={"Authorization": "TEST_DSH_AUTH"},
    )


@pytest.mark.asyncio
async def test_capabilities_validate_http_without_starting_event_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DSH_AUTH", "Bearer hidden")
    http = FakeDshHttp()
    ws = FakeWebSocketHub()
    gateway = DshWebGateway(
        transport=http.transport(),
        connect_ws=ws.connect,
    )
    try:
        result = await gateway.capabilities(snapshot())
        assert result.wire_version == "0.0.1"
        assert result.presets == ("standard", "minimal")
        assert result.plugins[0].entry_id == "tool-bash"
        assert ws.calls == []
        plugin_call = next(call for call in http.calls if call[0] == "pluginInventory/list")
        assert plugin_call[1]["payload"] == {"args": {}}
        assert plugin_call[2]["authorization"] == "Bearer hidden"
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "error_type"),
    [
        (
            {
                "type": "server-response",
                "rpcId": "wrong",
                "result": {"ok": True, "value": {}},
            },
            DshProtocolError,
        ),
        (
            {
                "type": "server-response",
                "rpcId": "wrong",
                "result": {
                    "ok": False,
                    "error": {"code": "offline", "message": "no", "details": {}},
                },
            },
            DshProtocolError,
        ),
    ],
)
async def test_rpc_rejects_mismatched_envelope(
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, Any],
    error_type: type[Exception],
) -> None:
    monkeypatch.setenv("TEST_DSH_AUTH", "secret")
    http = FakeDshHttp()
    http.override["host.describe"] = override
    gateway = DshWebGateway(
        transport=http.transport(),
        connect_ws=FakeWebSocketHub().connect,
    )
    try:
        with pytest.raises(error_type):
            await gateway.capabilities(snapshot())
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_rpc_surfaces_remote_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DSH_AUTH", "secret")
    http = FakeDshHttp()

    async def failing(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "type": "server-response",
                "rpcId": body["rpcId"],
                "result": {
                    "ok": False,
                    "error": {
                        "code": "agent-preset-not-found",
                        "message": "missing",
                        "details": {"preset": "nope"},
                    },
                },
            },
        )

    gateway = DshWebGateway(
        transport=httpx.MockTransport(failing),
        connect_ws=FakeWebSocketHub().connect,
    )
    try:
        with pytest.raises(DshRpcError, match="agent-preset-not-found") as exc:
            await gateway.open_session(
                snapshot(), cwd="/workspace", agent_preset="nope"
            )
        assert exc.value.details == {"preset": "nope"}
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_failed_session_open_stops_websocket_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DSH_AUTH", "secret")
    attempts = 0

    def connect(_url: str, **_kwargs: Any):
        nonlocal attempts
        attempts += 1
        raise OSError("DSH offline")

    gateway = DshWebGateway(
        transport=FakeDshHttp().transport(),
        connect_ws=connect,
    )
    try:
        with pytest.raises(RuntimeError, match="连接 DSH WebSocket 超时"):
            await gateway.open_session(
                replace(snapshot(), connect_timeout_seconds=0.05),
                cwd="/workspace",
                agent_preset="standard",
            )
        attempts_after_failure = attempts
        await asyncio.sleep(0.3)
        assert attempts == attempts_after_failure
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_sessions_share_streams_route_events_and_answer_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DSH_AUTH", "secret")
    http = FakeDshHttp()
    ws = FakeWebSocketHub()
    gateway = DshWebGateway(
        transport=http.transport(),
        connect_ws=ws.connect,
    )
    first = await gateway.open_session(
        snapshot(), cwd="/workspace/a", agent_preset="standard"
    )
    second = await gateway.open_session(
        snapshot(), cwd="/workspace/b", agent_preset="minimal"
    )
    first_events = first.events()
    second_events = second.events()
    try:
        assert len(ws.sockets["events.mux"]) == 1
        assert len(ws.sockets["events.host"]) == 1
        await first.prompt("inspect disk", mode="queue", client_time_zone="UTC")
        await first.prompt("also inspect GPU", mode="steer")

        event = {
            "type": "assistant/chunk",
            "seq": 1,
            "time": 1,
            "data": {
                "turn": 1,
                "step": 1,
                "chunk": {"type": "text-delta", "text": "working"},
            },
        }
        await ws.push(
            "events.mux",
            {"type": "session/event", "sessionId": first.session_id, "event": event},
        )
        await ws.push(
            "events.mux",
            {"type": "session/event", "sessionId": first.session_id, "event": event},
            rpc_id="duplicate",
        )
        await ws.push(
            "events.mux",
            {
                "type": "session/event",
                "sessionId": second.session_id,
                "event": {
                    "type": "turn/start",
                    "seq": 1,
                    "time": 1,
                    "data": {"turn": 1},
                },
            },
            rpc_id="second",
        )
        first_event = await asyncio.wait_for(anext(first_events), 1)
        second_event = await asyncio.wait_for(anext(second_events), 1)
        assert first_event.kind is AgentBackendEventKind.TEXT_DELTA
        assert first_event.data["text"] == "working"
        assert second_event.kind is AgentBackendEventKind.TURN_STARTED

        await ws.push(
            "events.mux",
            {
                "type": "session/event",
                "sessionId": first.session_id,
                "event": {
                    "type": "assistant/chunk",
                    "seq": 2,
                    "time": 2,
                    "data": {
                        "chunk": {"type": "reasoning-delta", "text": "private"}
                    },
                },
            },
            rpc_id="reasoning",
        )
        await ws.push(
            "events.mux",
            {
                "type": "approval/requested",
                "sessionId": first.session_id,
                "approvalId": "approval-1",
                "toolName": "bash",
                "reason": "write",
            },
            rpc_id="approval-rpc",
        )
        approval = await asyncio.wait_for(anext(first_events), 1)
        assert approval.kind is AgentBackendEventKind.APPROVAL_REQUESTED
        assert approval.request_id == "approval-rpc"
        await first.respond_approval(
            "approval-rpc", "approval-1", approved=True
        )

        await ws.push(
            "events.mux",
            {
                "type": "question/requested",
                "sessionId": first.session_id,
                "questions": [{"id": "q1", "question": "Which?", "options": []}],
            },
            rpc_id="question-rpc",
        )
        question = await asyncio.wait_for(anext(first_events), 1)
        assert question.kind is AgentBackendEventKind.QUESTION_REQUESTED
        await first.respond_question(
            "question-rpc",
            [
                {"id": "q1", "selected": [], "custom": "A"},
                {"id": "q2", "selected": ["B"], "custom": None},
            ],
        )
        await first.respond_question("question-rpc-2", None)
        await first.cancel()

        prompt_calls = [call for call in http.calls if call[0] == "session.prompt"]
        assert [call[1]["payload"]["mode"] for call in prompt_calls] == [
            "queue",
            "steer",
        ]
        responses = [call[1] for call in http.calls if call[0] == "respond"]
        assert responses[0]["result"]["value"]["outcome"] == "allowed-once"
        assert responses[1]["result"]["value"]["answer"]["answers"][0]["id"] == "q1"
        assert "custom" not in responses[1]["result"]["value"]["answer"]["answers"][1]
        assert responses[2]["result"]["error"]["code"] == "cancelled"
    finally:
        await first.close()
        await second.close()
        await gateway.close()


@pytest.mark.asyncio
async def test_last_session_close_stops_event_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DSH_AUTH", "secret")
    ws = FakeWebSocketHub()
    gateway = DshWebGateway(
        transport=FakeDshHttp().transport(),
        connect_ws=ws.connect,
    )
    try:
        session = await gateway.open_session(
            snapshot(), cwd="/workspace", agent_preset="standard"
        )
        assert len(ws.calls) == 2
        await session.close()
        await asyncio.sleep(0.3)
        assert len(ws.calls) == 2
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_reconnect_replays_history_and_deduplicates_seq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DSH_AUTH", "secret")
    http = FakeDshHttp()
    ws = FakeWebSocketHub()
    gateway = DshWebGateway(
        transport=http.transport(),
        connect_ws=ws.connect,
    )
    session = await gateway.open_session(
        snapshot(), cwd="/workspace", agent_preset="standard"
    )
    events = session.events()
    first_raw = {
        "type": "turn/start",
        "seq": 1,
        "time": 1,
        "data": {"turn": 1},
    }
    second_raw = {
        "type": "assistant/message",
        "seq": 2,
        "time": 2,
        "data": {
            "turn": 1,
            "step": 1,
            "message": {"content": [{"type": "text", "text": "done"}]},
        },
    }
    try:
        await ws.push(
            "events.mux",
            {
                "type": "session/event",
                "sessionId": session.session_id,
                "event": first_raw,
            },
        )
        assert (await asyncio.wait_for(anext(events), 1)).seq == 1
        http.history[session.session_id] = [
            {"event": first_raw},
            {"event": second_raw},
        ]
        await ws.sockets["events.mux"][0].close_from_server()
        await ws.wait_connected("events.mux", 2)
        replayed = await asyncio.wait_for(anext(events), 2)
        assert replayed.seq == 2
        assert replayed.kind is AgentBackendEventKind.MESSAGE_COMPLETED
        assert replayed.data["text"] == "done"
        assert any(call[0] == "session.history" for call in http.calls)
    finally:
        await session.close()
        await gateway.close()


@pytest.mark.asyncio
async def test_late_control_response_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DSH_AUTH", "secret")
    http = FakeDshHttp()
    http.receipt = {"accepted": False, "reason": "not-pending"}
    gateway = DshWebGateway(
        transport=http.transport(),
        connect_ws=FakeWebSocketHub().connect,
    )
    session = await gateway.open_session(
        snapshot(), cwd="/workspace", agent_preset="standard"
    )
    try:
        with pytest.raises(AgentBackendResponseRejected) as exc:
            await session.respond_approval("late", "approval", approved=False)
        assert exc.value.reason == "not-pending"
    finally:
        await session.close()
        await gateway.close()


def test_headers_env_never_stores_secret_and_requires_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_DSH_AUTH", raising=False)
    gateway = DshWebGateway(
        transport=FakeDshHttp().transport(),
        connect_ws=FakeWebSocketHub().connect,
    )

    async def run() -> None:
        with pytest.raises(RuntimeError, match="TEST_DSH_AUTH"):
            await gateway.capabilities(snapshot())
        await gateway.close()

    asyncio.run(run())
