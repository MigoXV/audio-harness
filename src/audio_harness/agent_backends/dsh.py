from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect as websocket_connect

from audio_harness.agent_backends.base import (
    AgentBackendGateway,
    AgentBackendSession,
)
from audio_harness.agent_backends.models import (
    AgentBackendCapabilities,
    AgentBackendEvent,
    AgentBackendEventKind,
    AgentBackendPlugin,
    AgentBackendProtocol,
    AgentBackendSnapshot,
)

logger = logging.getLogger(__name__)

_SUPPORTED_WIRE_VERSION = "0.0.1"
_CLOSED = object()


class DshProtocolError(RuntimeError):
    """DSH 返回了不符合 dsh_web_v1 的载荷。"""


class DshRpcError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"DSH RPC {code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})


class AgentBackendResponseRejected(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"DSH 已不再等待该回答: {reason}")
        self.reason = reason


def _required_string(value: Any, *, at: str) -> str:
    if not isinstance(value, str) or not value:
        raise DshProtocolError(f"{at} 必须是非空字符串")
    return value


def _required_mapping(value: Any, *, at: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DshProtocolError(f"{at} 必须是对象")
    return value


def _resolved_headers(snapshot: AgentBackendSnapshot) -> dict[str, str]:
    headers: dict[str, str] = {}
    missing: list[str] = []
    for header, env_name in snapshot.headers_env.items():
        value = os.environ.get(env_name)
        if value is None:
            missing.append(env_name)
        else:
            headers[header] = value
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise RuntimeError(f"Agent Backend 请求头环境变量未设置: {names}")
    return headers


def _api_url(target: str, path: str) -> str:
    return f"{target.rstrip('/')}/api/{path.lstrip('/')}"


def _websocket_url(target: str, path: str) -> str:
    parsed = urlsplit(_api_url(target, path))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, parsed.path, "", ""))


@dataclass
class _SessionState:
    queue: asyncio.Queue[AgentBackendEvent | object] = field(
        default_factory=lambda: asyncio.Queue(maxsize=512)
    )
    last_seq: int = -1
    pending_request_ids: set[str] = field(default_factory=set)
    approval_requests: dict[str, str] = field(default_factory=dict)


class _DshConnection:
    def __init__(
        self,
        snapshot: AgentBackendSnapshot,
        *,
        transport: httpx.AsyncBaseTransport | None,
        connect_ws: Callable[..., Any],
    ) -> None:
        self.snapshot = snapshot
        self._headers = _resolved_headers(snapshot)
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                snapshot.request_timeout_seconds,
                connect=snapshot.connect_timeout_seconds,
            ),
            follow_redirects=False,
        )
        self._connect_ws = connect_ws
        self._sessions: dict[str, _SessionState] = {}
        self._sessions_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._connected = {
            "events.mux": asyncio.Event(),
            "events.host": asyncio.Event(),
        }
        self._stream_errors: dict[str, BaseException] = {}
        self._mux_connections = 0
        self._closed = False
        self.retiring = False

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    async def ensure_started(self) -> None:
        if self._closed:
            raise RuntimeError("DSH 连接池已关闭")
        async with self._start_lock:
            for stream in self._connected:
                task = self._tasks.get(stream)
                if task is None or task.done():
                    self._tasks[stream] = asyncio.create_task(
                        self._stream_forever(stream),
                        name=f"dsh-{stream}-{self.snapshot.id}-{self.snapshot.revision}",
                    )
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(event.wait() for event in self._connected.values())
                ),
                timeout=self.snapshot.connect_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            messages = "; ".join(
                f"{name}: {error}" for name, error in self._stream_errors.items()
            )
            suffix = f" ({messages})" if messages else ""
            raise RuntimeError(f"连接 DSH WebSocket 超时{suffix}") from exc

    async def rpc(self, method: str, payload: Mapping[str, Any]) -> Any:
        rpc_id = str(uuid4())
        request = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": method,
            "payload": dict(payload),
        }
        response = await self._http.post(
            _api_url(self.snapshot.target, method),
            headers=self._headers,
            json=request,
        )
        response.raise_for_status()
        try:
            envelope = response.json()
        except ValueError as exc:
            raise DshProtocolError(
                f"{method} 返回的响应不是 JSON"
            ) from exc
        envelope = _required_mapping(envelope, at=f"{method} envelope")
        if envelope.get("type") != "server-response":
            raise DshProtocolError(f"{method} 响应 type 不是 server-response")
        if envelope.get("rpcId") != rpc_id:
            raise DshProtocolError(f"{method} 响应 rpcId 不匹配")
        result = _required_mapping(envelope.get("result"), at=f"{method}.result")
        if result.get("ok") is True:
            if "value" not in result:
                raise DshProtocolError(f"{method}.result 缺少 value")
            return result["value"]
        if result.get("ok") is False:
            error = _required_mapping(
                result.get("error"),
                at=f"{method}.result.error",
            )
            raise DshRpcError(
                _required_string(error.get("code"), at="error.code"),
                _required_string(error.get("message"), at="error.message"),
                error.get("details") if isinstance(error.get("details"), Mapping) else None,
            )
        raise DshProtocolError(f"{method}.result.ok 必须是布尔值")

    async def respond(self, rpc_id: str, result: Mapping[str, Any]) -> None:
        response = await self._http.post(
            _api_url(self.snapshot.target, "respond"),
            headers=self._headers,
            json={
                "type": "client-response",
                "rpcId": rpc_id,
                "result": dict(result),
            },
        )
        response.raise_for_status()
        try:
            receipt = response.json()
        except ValueError as exc:
            raise DshProtocolError("respond 返回的 receipt 不是 JSON") from exc
        receipt = _required_mapping(receipt, at="respond receipt")
        if receipt.get("accepted") is True:
            return
        if receipt.get("accepted") is False and receipt.get("reason") in {
            "not-pending",
            "bad-response",
        }:
            raise AgentBackendResponseRejected(str(receipt["reason"]))
        raise DshProtocolError("respond receipt 不符合 RpcReceipt")

    async def create_session(
        self,
        *,
        cwd: str,
        agent_preset: str,
    ) -> DshWebSession:
        await self.ensure_started()
        session_id = str(uuid4())
        state = _SessionState()
        async with self._sessions_lock:
            self._sessions[session_id] = state
        try:
            value = await self.rpc(
                "session.create",
                {
                    "sessionId": session_id,
                    "cwd": cwd,
                    "agentPreset": agent_preset,
                },
            )
            value = _required_mapping(value, at="session.create.value")
            actual_id = _required_string(
                value.get("sessionId"),
                at="session.create.value.sessionId",
            )
            if actual_id != session_id:
                raise DshProtocolError("session.create 未采用预分配 sessionId")
        except BaseException:
            async with self._sessions_lock:
                self._sessions.pop(session_id, None)
            raise
        return DshWebSession(self, session_id, state)

    async def release_session(self, session_id: str) -> None:
        async with self._sessions_lock:
            state = self._sessions.pop(session_id, None)
            should_close = self.retiring and not self._sessions
        if state is not None:
            self._put_nowait(state, _CLOSED)
        if should_close:
            await self.close()

    def _put_nowait(
        self,
        state: _SessionState,
        event: AgentBackendEvent | object,
    ) -> None:
        if state.queue.full():
            with suppress(asyncio.QueueEmpty):
                state.queue.get_nowait()
        state.queue.put_nowait(event)

    async def _emit(
        self,
        session_id: str,
        event: AgentBackendEvent,
    ) -> None:
        state = self._sessions.get(session_id)
        if state is not None:
            self._put_nowait(state, event)

    async def _stream_forever(self, stream: str) -> None:
        delay = 0.25
        while not self._closed:
            try:
                async with self._connect_ws(
                    _websocket_url(self.snapshot.target, stream),
                    additional_headers=self._headers,
                    open_timeout=self.snapshot.connect_timeout_seconds,
                    close_timeout=2,
                ) as websocket:
                    reconnecting = stream == "events.mux" and self._mux_connections > 0
                    if stream == "events.mux":
                        self._mux_connections += 1
                    self._stream_errors.pop(stream, None)
                    self._connected[stream].set()
                    delay = 0.25
                    if reconnecting:
                        await self._replay_histories()
                    async for raw in websocket:
                        await self._handle_stream_message(stream, raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._stream_errors[stream] = exc
                if self._closed:
                    return
                logger.warning(
                    "DSH %s WebSocket 断开，将重连: %s",
                    stream,
                    exc,
                )
            finally:
                self._connected[stream].clear()
            if not self._closed:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

    async def _handle_stream_message(self, stream: str, raw: Any) -> None:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DshProtocolError(f"{stream} 收到非 UTF-8 帧") from exc
        if not isinstance(raw, str):
            raise DshProtocolError(f"{stream} 收到未知 WebSocket 帧")
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DshProtocolError(f"{stream} 收到无效 JSON") from exc
        envelope = _required_mapping(envelope, at=f"{stream} envelope")
        if envelope.get("type") != "server-request":
            raise DshProtocolError(f"{stream} envelope.type 无效")
        rpc_id = _required_string(envelope.get("rpcId"), at="stream.rpcId")
        payload = _required_mapping(envelope.get("payload"), at="stream.payload")
        if envelope.get("method") != payload.get("type"):
            raise DshProtocolError(f"{stream} envelope.method 与 payload.type 不匹配")
        if stream == "events.mux":
            await self._handle_mux(rpc_id, payload)
        else:
            await self._handle_host(payload)

    async def _handle_mux(
        self,
        rpc_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        frame_type = payload.get("type")
        if frame_type == "stream/error":
            error = payload.get("error")
            for session_id in tuple(self._sessions):
                await self._emit(
                    session_id,
                    AgentBackendEvent(
                        kind=AgentBackendEventKind.ERROR,
                        session_id=session_id,
                        data={"error": error},
                    ),
                )
            return
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str):
            return
        state = self._sessions.get(session_id)
        if state is None:
            return
        if frame_type == "session/event":
            raw_event = _required_mapping(payload.get("event"), at="session.event")
            event = self._map_session_event(session_id, raw_event, payload.get("view"))
            if event is None:
                return
            if event.seq is not None:
                if event.seq <= state.last_seq:
                    return
                state.last_seq = event.seq
            self._put_nowait(state, event)
            return
        if frame_type == "approval/requested":
            if rpc_id in state.pending_request_ids:
                return
            state.pending_request_ids.add(rpc_id)
            approval_id = _required_string(
                payload.get("approvalId"), at="approvalId"
            )
            state.approval_requests[approval_id] = rpc_id
            self._put_nowait(
                state,
                AgentBackendEvent(
                    kind=AgentBackendEventKind.APPROVAL_REQUESTED,
                    session_id=session_id,
                    request_id=rpc_id,
                    data={
                        "approval_id": approval_id,
                        "tool_name": payload.get("toolName"),
                        "call_id": payload.get("callId"),
                        "reason": payload.get("reason"),
                    },
                ),
            )
            return
        if frame_type == "approval/resolved":
            approval_id = str(payload.get("approvalId", ""))
            request_id = state.approval_requests.pop(approval_id, None)
            if request_id:
                state.pending_request_ids.discard(request_id)
            self._put_nowait(
                state,
                AgentBackendEvent(
                    kind=AgentBackendEventKind.APPROVAL_RESOLVED,
                    session_id=session_id,
                    request_id=request_id,
                    data={
                        "approval_id": approval_id,
                        "outcome": payload.get("outcome"),
                    },
                ),
            )
            return
        if frame_type == "question/requested":
            if rpc_id in state.pending_request_ids:
                return
            state.pending_request_ids.add(rpc_id)
            self._put_nowait(
                state,
                AgentBackendEvent(
                    kind=AgentBackendEventKind.QUESTION_REQUESTED,
                    session_id=session_id,
                    request_id=rpc_id,
                    data={"questions": payload.get("questions", [])},
                ),
            )
            return
        if frame_type == "question/resolved":
            request_id = payload.get("questionRpcId")
            if isinstance(request_id, str):
                state.pending_request_ids.discard(request_id)
            self._put_nowait(
                state,
                AgentBackendEvent(
                    kind=AgentBackendEventKind.QUESTION_RESOLVED,
                    session_id=session_id,
                    request_id=request_id if isinstance(request_id, str) else None,
                    data={"outcome": payload.get("outcome")},
                ),
            )

    async def _handle_host(self, payload: Mapping[str, Any]) -> None:
        frame_type = payload.get("type")
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str) or session_id not in self._sessions:
            return
        if frame_type == "host/session-status":
            await self._emit(
                session_id,
                AgentBackendEvent(
                    kind=AgentBackendEventKind.SESSION_STATUS,
                    session_id=session_id,
                    data={"running": bool(payload.get("running"))},
                ),
            )
        elif frame_type == "host/agent-error":
            await self._emit(
                session_id,
                AgentBackendEvent(
                    kind=AgentBackendEventKind.ERROR,
                    session_id=session_id,
                    data={"message": str(payload.get("message", "DSH agent error"))},
                ),
            )

    def _map_session_event(
        self,
        session_id: str,
        raw: Mapping[str, Any],
        view: Any = None,
    ) -> AgentBackendEvent | None:
        event_type = raw.get("type")
        seq = raw.get("seq")
        if not isinstance(event_type, str) or not isinstance(seq, int):
            raise DshProtocolError("session event 缺少合法 type/seq")
        data_value = raw.get("data")
        data = dict(data_value) if isinstance(data_value, Mapping) else {}
        kind: AgentBackendEventKind
        mapped: dict[str, Any]
        if event_type == "turn/start":
            kind, mapped = AgentBackendEventKind.TURN_STARTED, data
        elif event_type == "turn/end":
            kind, mapped = AgentBackendEventKind.TURN_COMPLETED, data
        elif event_type == "assistant/chunk":
            chunk = data.get("chunk")
            if not isinstance(chunk, Mapping) or chunk.get("type") != "text-delta":
                return None
            text = chunk.get("text")
            if not isinstance(text, str):
                return None
            kind = AgentBackendEventKind.TEXT_DELTA
            mapped = {"text": text, "turn": data.get("turn"), "step": data.get("step")}
        elif event_type == "assistant/message":
            message = data.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            text = "".join(
                str(block.get("text", ""))
                for block in content or []
                if isinstance(block, Mapping) and block.get("type") == "text"
            )
            kind = AgentBackendEventKind.MESSAGE_COMPLETED
            mapped = {
                "text": text,
                "turn": data.get("turn"),
                "step": data.get("step"),
                "usage": data.get("usage"),
            }
        elif event_type == "tool/call":
            kind = AgentBackendEventKind.TOOL_STARTED
            mapped = {
                "call_id": data.get("callId"),
                "name": data.get("name"),
                "arguments": data.get("arguments"),
                "view": view,
            }
        elif event_type == "tool/result":
            message = data.get("message")
            source = message.get("source") if isinstance(message, Mapping) else None
            kind = AgentBackendEventKind.TOOL_COMPLETED
            mapped = {
                "call_id": (
                    source.get("callId") if isinstance(source, Mapping) else None
                ),
                "message": message,
                "error": data.get("error"),
                "meta": data.get("meta"),
                "view": view,
            }
        else:
            return None
        return AgentBackendEvent(
            kind=kind,
            session_id=session_id,
            seq=seq,
            data=mapped,
        )

    async def _replay_histories(self) -> None:
        for session_id in tuple(self._sessions):
            try:
                value = await self.rpc(
                    "session.history",
                    {"sessionId": session_id, "maxMessages": 100},
                )
                value = _required_mapping(value, at="session.history.value")
                entries = value.get("events")
                if not isinstance(entries, list):
                    raise DshProtocolError("session.history.value.events 必须是数组")
                for entry in entries:
                    entry = _required_mapping(entry, at="session.history entry")
                    await self._handle_mux(
                        f"history:{session_id}",
                        {
                            "type": "session/event",
                            "sessionId": session_id,
                            "event": entry.get("event"),
                            "view": entry.get("view"),
                        },
                    )
            except Exception as exc:
                await self._emit(
                    session_id,
                    AgentBackendEvent(
                        kind=AgentBackendEventKind.ERROR,
                        session_id=session_id,
                        data={"message": f"DSH history replay 失败: {exc}"},
                    ),
                )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        for state in self._sessions.values():
            self._put_nowait(state, _CLOSED)
        self._sessions.clear()
        await self._http.aclose()


class DshWebSession(AgentBackendSession):
    def __init__(
        self,
        connection: _DshConnection,
        session_id: str,
        state: _SessionState,
    ) -> None:
        self._connection = connection
        self._session_id = session_id
        self._state = state
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("DSH session 已关闭")

    async def prompt(
        self,
        text: str,
        *,
        mode: Literal["queue", "steer"] = "queue",
        client_time_zone: str | None = None,
    ) -> None:
        self._ensure_open()
        if mode not in {"queue", "steer"}:
            raise ValueError("DSH prompt mode 必须是 queue 或 steer")
        payload: dict[str, Any] = {
            "sessionId": self._session_id,
            "mode": mode,
            "content": [{"type": "text", "text": text}],
        }
        if client_time_zone:
            payload["clientTimeZone"] = client_time_zone
        value = await self._connection.rpc("session.prompt", payload)
        value = _required_mapping(value, at="session.prompt.value")
        if value.get("accepted") is not True:
            raise DshProtocolError("session.prompt 未被 DSH 接受")

    async def cancel(self) -> None:
        self._ensure_open()
        await self._connection.rpc(
            "session.cancel",
            {"sessionId": self._session_id},
        )

    async def respond_approval(
        self,
        request_id: str,
        approval_id: str,
        *,
        approved: bool,
    ) -> None:
        self._ensure_open()
        await self._connection.respond(
            request_id,
            {
                "ok": True,
                "value": {
                    "sessionId": self._session_id,
                    "approvalId": approval_id,
                    "outcome": "allowed-once" if approved else "rejected",
                },
            },
        )

    async def respond_question(
        self,
        request_id: str,
        answers: Sequence[Mapping[str, Any]] | None,
    ) -> None:
        self._ensure_open()
        if answers is None:
            result: dict[str, Any] = {
                "ok": False,
                "error": {
                    "code": "cancelled",
                    "message": "Question timed out or was cancelled",
                    "details": {},
                },
            }
        else:
            normalized_answers = [
                {
                    key: value
                    for key, value in dict(answer).items()
                    if value is not None
                }
                for answer in answers
            ]
            result = {
                "ok": True,
                "value": {
                    "sessionId": self._session_id,
                    "answer": {"answers": normalized_answers},
                },
            }
        await self._connection.respond(request_id, result)

    async def _events(self) -> AsyncIterator[AgentBackendEvent]:
        while True:
            event = await self._state.queue.get()
            if event is _CLOSED:
                return
            assert isinstance(event, AgentBackendEvent)
            yield event

    def events(self) -> AsyncIterator[AgentBackendEvent]:
        return self._events()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._connection.release_session(self._session_id)


class DshWebGateway(AgentBackendGateway):
    """按 backend snapshot 共享 DSH HTTP 客户端和两条事件 WebSocket。"""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_ws: Callable[..., Any] = websocket_connect,
    ) -> None:
        self._transport = transport
        self._connect_ws = connect_ws
        self._connections: dict[tuple[str, int], _DshConnection] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def _connection(
        self,
        snapshot: AgentBackendSnapshot,
    ) -> _DshConnection:
        if snapshot.protocol is not AgentBackendProtocol.DSH_WEB_V1:
            raise RuntimeError(f"不支持的 Agent Backend 协议: {snapshot.protocol.value}")
        key = (snapshot.id, snapshot.revision)
        async with self._lock:
            if self._closed:
                raise RuntimeError("Agent Backend gateway 已关闭")
            connection = self._connections.get(key)
            if connection is None:
                connection = _DshConnection(
                    snapshot,
                    transport=self._transport,
                    connect_ws=self._connect_ws,
                )
                self._connections[key] = connection
            return connection

    async def capabilities(
        self,
        snapshot: AgentBackendSnapshot,
    ) -> AgentBackendCapabilities:
        connection = await self._connection(snapshot)
        await connection.ensure_started()
        host, preset_value, plugin_value = await asyncio.gather(
            connection.rpc("host.describe", {}),
            connection.rpc("agentPreset.list", {}),
            connection.rpc("pluginInventory/list", {"args": {}}),
        )
        host = _required_mapping(host, at="host.describe.value")
        preset_value = _required_mapping(
            preset_value, at="agentPreset.list.value"
        )
        plugin_value = _required_mapping(
            plugin_value, at="pluginInventory/list.value"
        )
        wire_version = _required_string(host.get("version"), at="host.version")
        if wire_version != _SUPPORTED_WIRE_VERSION:
            raise DshProtocolError(
                f"不支持的 DSH wire version: {wire_version}"
            )
        raw_presets = preset_value.get("presets")
        if not isinstance(raw_presets, list):
            raise DshProtocolError("agentPreset.list.value.presets 必须是数组")
        presets = tuple(
            _required_string(
                _required_mapping(item, at="preset").get("id"),
                at="preset.id",
            )
            for item in raw_presets
        )
        raw_plugins = plugin_value.get("entries")
        if not isinstance(raw_plugins, list):
            raise DshProtocolError("pluginInventory/list.value.entries 必须是数组")
        plugins: list[AgentBackendPlugin] = []
        for raw_plugin in raw_plugins:
            plugin = _required_mapping(raw_plugin, at="plugin entry")
            plugins.append(
                AgentBackendPlugin(
                    entry_id=_required_string(
                        plugin.get("entryId"), at="plugin.entryId"
                    ),
                    module_name=_required_string(
                        plugin.get("moduleName"), at="plugin.moduleName"
                    ),
                    enabled=plugin.get("enabled") is True,
                    phase=(
                        str(plugin["fiberPhase"])
                        if plugin.get("fiberPhase") is not None
                        else None
                    ),
                )
            )
        return AgentBackendCapabilities(
            wire_version=wire_version,
            provider=_required_string(host.get("provider"), at="host.provider"),
            model=_required_string(host.get("model"), at="host.model"),
            cwd=_required_string(host.get("cwd"), at="host.cwd"),
            presets=presets,
            plugins=tuple(plugins),
        )

    async def open_session(
        self,
        snapshot: AgentBackendSnapshot,
        *,
        cwd: str,
        agent_preset: str,
    ) -> AgentBackendSession:
        connection = await self._connection(snapshot)
        return await connection.create_session(
            cwd=cwd,
            agent_preset=agent_preset,
        )

    async def retire(
        self,
        backend_id: str,
        *,
        before_revision: int | None = None,
    ) -> None:
        async with self._lock:
            selected = [
                (key, connection)
                for key, connection in self._connections.items()
                if key[0] == backend_id
                and (before_revision is None or key[1] < before_revision)
            ]
            for key, connection in selected:
                connection.retiring = True
                self._connections.pop(key, None)
        for _, connection in selected:
            if connection.active_sessions == 0:
                await connection.close()

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            connections = list(self._connections.values())
            self._connections.clear()
        await asyncio.gather(
            *(connection.close() for connection in connections),
            return_exceptions=True,
        )
