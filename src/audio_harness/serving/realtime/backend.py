from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
import json
from typing import Any, Literal
from uuid import uuid4

from audio_harness.agent_backends.base import (
    AgentBackendGateway,
    AgentBackendSession,
)
from audio_harness.agent_backends.dsh import AgentBackendResponseRejected
from audio_harness.agent_backends.models import (
    AgentBackendEvent,
    AgentBackendEventKind,
    AgentBackendSnapshot,
)
from audio_harness.voice_agents.agent_backend_context import (
    agent_backend_system_context_instructions,
)
from audio_harness.voice_agents.models import AgentBackendBinding

EventSender = Callable[[dict[str, Any]], Awaitable[None]]
TextSender = Callable[[str], Awaitable[None]]

_MAX_DELTA_CHARS = 2_000
_MAX_ARGUMENT_CHARS = 4_096
_MAX_RESULT_CHARS = 8_192
_MAX_FINAL_CHARS = 16_384
_MAX_SUMMARY_TOOL_CHARS = 32_768


@dataclass(frozen=True)
class AgentBackendTurnResult:
    backend_session_id: str
    final_text: str
    tool_results: tuple[str, ...]


@dataclass
class _PendingInteraction:
    kind: Literal["approval", "question"]
    request_id: str
    approval_id: str | None
    payload: dict[str, Any]
    timeout_task: asyncio.Task[None] | None = None


def _bounded_text(value: Any, limit: int) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _safe_questions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    questions: list[dict[str, Any]] = []
    for raw in value[:20]:
        if not isinstance(raw, Mapping):
            continue
        options = raw.get("options")
        safe_options: list[dict[str, Any]] = []
        if isinstance(options, list):
            for option in options[:50]:
                if not isinstance(option, Mapping):
                    continue
                safe_options.append(
                    {
                        "label": _bounded_text(option.get("label", ""), 500),
                        **(
                            {"description": _bounded_text(option["description"], 1_000)}
                            if option.get("description") is not None
                            else {}
                        ),
                    }
                )
        questions.append(
            {
                "id": _bounded_text(raw.get("id", ""), 200),
                "question": _bounded_text(raw.get("question", ""), 1_000),
                "options": safe_options,
                "multiSelect": raw.get("multiSelect") is True,
            }
        )
    return questions


class RealtimeAgentBackendRuntime:
    """一条 Realtime/WebRTC 连接内的 Agent Backend 会话状态。"""

    def __init__(
        self,
        *,
        snapshot: AgentBackendSnapshot,
        binding: AgentBackendBinding,
        gateway: AgentBackendGateway,
        send_event: EventSender,
        speak_notice: TextSender,
        summarize: Callable[[AgentBackendTurnResult], Awaitable[None]],
        extension_events_enabled: bool,
        system_context: str | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.binding = binding
        self._gateway = gateway
        self._send_event = send_event
        self._speak_notice = speak_notice
        self._summarize = summarize
        self._extension_events_enabled = extension_events_enabled
        self._system_context = (
            system_context
            or agent_backend_system_context_instructions(
                snapshot,
                binding,
            )
        )
        self._session: AgentBackendSession | None = None
        self._session_lock = asyncio.Lock()
        self._event_task: asyncio.Task[None] | None = None
        self._task_timeout_task: asyncio.Task[None] | None = None
        self._pending: dict[str, _PendingInteraction] = {}
        self._pending_lock = asyncio.Lock()
        self._running = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self._input_finished = False
        self._final_text = ""
        self._tool_results: list[str] = []

    @property
    def running(self) -> bool:
        return self._running

    @property
    def session_id(self) -> str | None:
        return self._session.session_id if self._session is not None else None

    def context(self) -> dict[str, Any]:
        pending = [
            {
                "kind": item.kind,
                "request_id": item.request_id,
                "approval_id": item.approval_id,
                **item.payload,
            }
            for item in self._pending.values()
        ]
        return {
            "backend_id": self.snapshot.id,
            "backend": {
                "name": self.snapshot.name,
                "kind": self.snapshot.kind.value,
                "agent_preset": self.binding.agent_preset,
            },
            "backend_session_id": self.session_id,
            "running": self._running,
            "pending_interactions": pending,
        }

    def response_context_instructions(self) -> str:
        if self._pending:
            status = "等待用户处理后台审批或结构化问题"
        elif self._running:
            status = "后台任务执行中"
        elif self._session is not None:
            status = "后台会话已建立，当前空闲"
        else:
            status = "后台会话尚未建立"
        return (
            "\n\n" + self._system_context
            + "\n\n[Audio Harness Agent Backend 运行状态]\n"
            + f"当前状态：{status}。"
        )

    def latest_pending(self) -> _PendingInteraction | None:
        return next(reversed(self._pending.values()), None)

    async def ensure_session(self) -> AgentBackendSession:
        if self._closed:
            raise RuntimeError("Agent Backend 会话已经关闭")
        if self._session is not None:
            return self._session
        async with self._session_lock:
            if self._session is None:
                session = await self._gateway.open_session(
                    self.snapshot,
                    cwd=self.binding.cwd,
                    agent_preset=self.binding.agent_preset,
                )
                self._session = session
                self._event_task = asyncio.create_task(
                    self._consume_events(session),
                    name=f"agent-backend-events-{session.session_id}",
                )
                await self._emit(
                    "x_aha.agent_backend.session",
                    status="created",
                )
        assert self._session is not None
        return self._session

    async def delegate(
        self,
        task: str,
        *,
        mode: Literal["queue", "steer"],
    ) -> None:
        session = await self.ensure_session()
        self._running = True
        self._idle.clear()
        if mode == "queue":
            self._final_text = ""
            self._tool_results.clear()
        self._reset_task_timeout()
        try:
            await session.prompt(task, mode=mode)
        except BaseException:
            if mode == "queue":
                self._running = False
                self._idle.set()
            raise

    async def cancel(self, *, reason: str = "user_requested") -> bool:
        session = self._session
        if session is None or not self._running:
            return False
        await session.cancel()
        self._running = False
        self._cancel_task_timeout()
        await self._emit(
            "x_aha.agent_backend.cancelled",
            code=reason,
            message="Agent Backend 任务已取消",
        )
        self._idle.set()
        return True

    async def submit_approval(
        self,
        request_id: str,
        approval_id: str,
        *,
        approved: bool,
    ) -> bool:
        pending = await self._claim_pending(request_id, "approval")
        if pending is None or pending.approval_id != approval_id:
            await self._emit_approval_resolution(
                request_id,
                approval_id,
                outcome=None,
                resolution="already_processed",
            )
            return False
        assert self._session is not None
        try:
            await self._session.respond_approval(
                request_id,
                approval_id,
                approved=approved,
            )
        except AgentBackendResponseRejected:
            await self._emit_approval_resolution(
                request_id,
                approval_id,
                outcome=None,
                resolution="already_processed",
            )
            return False
        await self._emit_approval_resolution(
            request_id,
            approval_id,
            outcome="allowed-once" if approved else "rejected",
            resolution="handled",
        )
        return True

    async def submit_question(
        self,
        request_id: str,
        answers: Sequence[Mapping[str, Any]],
    ) -> bool:
        pending = await self._claim_pending(request_id, "question")
        if pending is None:
            await self._emit_question_resolution(
                request_id,
                outcome=None,
                resolution="already_processed",
            )
            return False
        assert self._session is not None
        try:
            await self._session.respond_question(
                request_id,
                self._normalize_question_answers(pending, answers),
            )
        except AgentBackendResponseRejected:
            await self._emit_question_resolution(
                request_id,
                outcome=None,
                resolution="already_processed",
            )
            return False
        await self._emit_question_resolution(
            request_id,
            outcome="answered",
            resolution="handled",
        )
        return True

    @staticmethod
    def _normalize_question_answers(
        pending: _PendingInteraction,
        answers: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        questions = {
            str(question.get("id") or ""): question
            for question in pending.payload.get("questions", [])
            if isinstance(question, Mapping)
        }
        normalized: list[dict[str, Any]] = []
        for answer in answers:
            answer_id = str(answer.get("id") or "")
            question = questions.get(answer_id, {})
            selected_value = answer.get("selected")
            selected = (
                [str(value) for value in selected_value]
                if isinstance(selected_value, list)
                else []
            )
            custom_value = answer.get("custom")
            custom = (
                str(custom_value).strip()
                if isinstance(custom_value, str)
                else ""
            )
            item: dict[str, Any] = {"id": answer_id, "selected": selected}
            if custom and (question.get("multiSelect") is True or not selected):
                item["custom"] = custom
            normalized.append(item)
        return normalized

    async def _claim_pending(
        self,
        request_id: str,
        kind: Literal["approval", "question"],
    ) -> _PendingInteraction | None:
        async with self._pending_lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.kind != kind:
                return None
            self._pending.pop(request_id, None)
        if pending.timeout_task is not None:
            pending.timeout_task.cancel()
        return pending

    async def _consume_events(self, session: AgentBackendSession) -> None:
        try:
            async for event in session.events():
                await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._running = False
            await self._emit(
                "x_aha.agent_backend.failed",
                code="event_stream_failed",
                message=_bounded_text(exc, 1_000),
            )
            self._idle.set()

    async def _handle_event(self, event: AgentBackendEvent) -> None:
        self._idle.clear()
        try:
            await self._dispatch_event(event)
        finally:
            if not self._running:
                self._idle.set()

    async def _dispatch_event(self, event: AgentBackendEvent) -> None:
        kind = event.kind
        if kind is AgentBackendEventKind.SESSION_STATUS:
            await self._emit(
                "x_aha.agent_backend.session",
                status="running" if event.data.get("running") else "idle",
            )
        elif kind is AgentBackendEventKind.TURN_STARTED:
            self._running = True
            self._final_text = ""
            self._tool_results.clear()
            await self._emit(
                "x_aha.agent_backend.turn.started",
                seq=event.seq,
                turn=event.data.get("turn"),
            )
        elif kind is AgentBackendEventKind.TEXT_DELTA:
            await self._emit(
                "x_aha.agent_backend.text.delta",
                seq=event.seq,
                delta=_bounded_text(event.data.get("text", ""), _MAX_DELTA_CHARS),
            )
        elif kind is AgentBackendEventKind.MESSAGE_COMPLETED:
            text = str(event.data.get("text") or "")
            if text:
                self._final_text = _bounded_text(text, _MAX_FINAL_CHARS)
        elif kind is AgentBackendEventKind.TOOL_STARTED:
            await self._emit(
                "x_aha.agent_backend.tool.started",
                seq=event.seq,
                call_id=(
                    str(event.data["call_id"]) if event.data.get("call_id") else None
                ),
                name=(str(event.data["name"]) if event.data.get("name") else None),
                arguments=_bounded_text(
                    event.data.get("arguments", ""),
                    _MAX_ARGUMENT_CHARS,
                ),
            )
        elif kind is AgentBackendEventKind.TOOL_COMPLETED:
            result = _bounded_text(event.data.get("message", ""), _MAX_RESULT_CHARS)
            self._tool_results.append(result)
            while (
                len(self._tool_results) > 1
                and sum(map(len, self._tool_results)) > _MAX_SUMMARY_TOOL_CHARS
            ):
                self._tool_results.pop(0)
            await self._emit(
                "x_aha.agent_backend.tool.completed",
                seq=event.seq,
                call_id=(
                    str(event.data["call_id"]) if event.data.get("call_id") else None
                ),
                result=result,
                error=(
                    _bounded_text(event.data["error"], 1_000)
                    if event.data.get("error") is not None
                    else None
                ),
            )
        elif kind is AgentBackendEventKind.APPROVAL_REQUESTED:
            await self._register_approval(event)
        elif kind is AgentBackendEventKind.APPROVAL_RESOLVED:
            request_id = event.request_id
            if request_id:
                pending = await self._claim_pending(request_id, "approval")
                if pending is not None:
                    await self._emit_approval_resolution(
                        request_id,
                        str(event.data.get("approval_id") or ""),
                        outcome=(
                            str(event.data["outcome"])
                            if event.data.get("outcome") is not None
                            else None
                        ),
                        resolution="handled",
                    )
        elif kind is AgentBackendEventKind.QUESTION_REQUESTED:
            await self._register_question(event)
        elif kind is AgentBackendEventKind.QUESTION_RESOLVED:
            request_id = event.request_id
            if request_id:
                pending = await self._claim_pending(request_id, "question")
                if pending is not None:
                    await self._emit_question_resolution(
                        request_id,
                        outcome=(
                            str(event.data["outcome"])
                            if event.data.get("outcome") is not None
                            else None
                        ),
                        resolution="handled",
                    )
        elif kind is AgentBackendEventKind.ERROR:
            await self._emit(
                "x_aha.agent_backend.failed",
                code="backend_error",
                message=_bounded_text(event.data, 1_000),
            )
        elif kind is AgentBackendEventKind.TURN_COMPLETED:
            self._running = False
            self._cancel_task_timeout()
            reason = event.data.get("reason")
            await self._emit(
                "x_aha.agent_backend.turn.completed",
                seq=event.seq,
                turn=event.data.get("turn"),
                reason=reason,
            )
            reason_kind = reason.get("kind") if isinstance(reason, Mapping) else reason
            if reason_kind == "cancelled":
                await self._emit(
                    "x_aha.agent_backend.cancelled",
                    code="backend_cancelled",
                    message="Agent Backend 已取消任务",
                )
            elif self._final_text or self._tool_results:
                await self._summarize(
                    AgentBackendTurnResult(
                        backend_session_id=event.session_id,
                        final_text=self._final_text,
                        tool_results=tuple(self._tool_results),
                    )
                )

    async def _register_approval(self, event: AgentBackendEvent) -> None:
        if self._input_finished:
            await self.cancel(reason="client_input_closed")
            await self._emit(
                "x_aha.agent_backend.failed",
                code="client_input_closed",
                message="请求流已结束，无法继续处理后台交互",
            )
            return
        if not event.request_id:
            return
        approval_id = str(event.data.get("approval_id") or "")
        pending = _PendingInteraction(
            kind="approval",
            request_id=event.request_id,
            approval_id=approval_id,
            payload={
                "tool_name": event.data.get("tool_name"),
                "call_id": event.data.get("call_id"),
                "reason": _bounded_text(event.data.get("reason", ""), 1_000),
            },
        )
        await self._store_pending(pending)
        await self._emit(
            "x_aha.agent_backend.approval.requested",
            request_id=pending.request_id,
            approval_id=approval_id,
            **pending.payload,
        )
        tool_name = pending.payload.get("tool_name") or "后台工具"
        reason = pending.payload.get("reason")
        suffix = f"，原因是{reason}" if reason else ""
        await self._speak_notice(
            f"{tool_name} 请求执行审批{suffix}。你可以说允许或拒绝。"
        )

    async def _register_question(self, event: AgentBackendEvent) -> None:
        if self._input_finished:
            await self.cancel(reason="client_input_closed")
            await self._emit(
                "x_aha.agent_backend.failed",
                code="client_input_closed",
                message="请求流已结束，无法继续处理后台交互",
            )
            return
        if not event.request_id:
            return
        questions = _safe_questions(event.data.get("questions"))
        pending = _PendingInteraction(
            kind="question",
            request_id=event.request_id,
            approval_id=None,
            payload={"questions": questions},
        )
        await self._store_pending(pending)
        await self._emit(
            "x_aha.agent_backend.question.requested",
            request_id=pending.request_id,
            questions=questions,
        )
        prompt = "；".join(
            str(question.get("question") or "") for question in questions[:3]
        )
        await self._speak_notice(
            ("后台智能体需要你回答：" + prompt)
            if prompt
            else "后台智能体需要你补充一些信息。"
        )

    async def _store_pending(self, pending: _PendingInteraction) -> None:
        async with self._pending_lock:
            existing = self._pending.get(pending.request_id)
            if existing is not None:
                return
            self._pending[pending.request_id] = pending
            pending.timeout_task = asyncio.create_task(
                self._expire_pending(pending.request_id),
                name=f"agent-backend-interaction-timeout-{pending.request_id}",
            )

    async def _expire_pending(self, request_id: str) -> None:
        try:
            await asyncio.sleep(self.binding.interaction_timeout_seconds)
            pending = await self._claim_pending_for_timeout(request_id)
            if pending is None or self._session is None:
                return
            if pending.kind == "approval":
                assert pending.approval_id is not None
                with suppress(AgentBackendResponseRejected):
                    await self._session.respond_approval(
                        request_id,
                        pending.approval_id,
                        approved=False,
                    )
                await self._emit_approval_resolution(
                    request_id,
                    pending.approval_id,
                    outcome="rejected",
                    resolution="timed_out",
                )
            else:
                with suppress(AgentBackendResponseRejected):
                    await self._session.respond_question(request_id, None)
                await self._emit_question_resolution(
                    request_id,
                    outcome="cancelled",
                    resolution="timed_out",
                )
        except asyncio.CancelledError:
            raise

    async def _claim_pending_for_timeout(
        self,
        request_id: str,
    ) -> _PendingInteraction | None:
        async with self._pending_lock:
            return self._pending.pop(request_id, None)

    def _reset_task_timeout(self) -> None:
        self._cancel_task_timeout()
        self._task_timeout_task = asyncio.create_task(
            self._expire_task(),
            name=f"agent-backend-task-timeout-{self.snapshot.id}",
        )

    def _cancel_task_timeout(self) -> None:
        if self._task_timeout_task is not None:
            self._task_timeout_task.cancel()
            self._task_timeout_task = None

    async def _expire_task(self) -> None:
        try:
            await asyncio.sleep(self.binding.task_timeout_seconds)
            if self._session is None or not self._running:
                return
            await self._session.cancel()
            self._running = False
            await self._emit(
                "x_aha.agent_backend.failed",
                code="task_timeout",
                message="Agent Backend 任务超时并已取消",
            )
            await self._speak_notice("后台任务超时，我已经取消了它。")
            self._idle.set()
        except asyncio.CancelledError:
            raise

    async def _emit_approval_resolution(
        self,
        request_id: str,
        approval_id: str,
        *,
        outcome: str | None,
        resolution: Literal["handled", "timed_out", "already_processed"],
    ) -> None:
        await self._emit(
            "x_aha.agent_backend.approval.resolved",
            request_id=request_id,
            approval_id=approval_id,
            outcome=outcome,
            resolution=resolution,
        )

    async def _emit_question_resolution(
        self,
        request_id: str,
        *,
        outcome: str | None,
        resolution: Literal["handled", "timed_out", "already_processed"],
    ) -> None:
        await self._emit(
            "x_aha.agent_backend.question.resolved",
            request_id=request_id,
            outcome=outcome,
            resolution=resolution,
        )

    async def _emit(self, event_type: str, **payload: Any) -> None:
        if not self._extension_events_enabled:
            return
        event = {
            "type": event_type,
            "event_id": f"event_{uuid4().hex}",
            "backend_id": self.snapshot.id,
            "backend_session_id": self.session_id,
            **payload,
        }
        await self._send_event(event)

    async def finish_input(self) -> None:
        self._input_finished = True
        if self._pending:
            await self.cancel(reason="client_input_closed")
            await self._emit(
                "x_aha.agent_backend.failed",
                code="client_input_closed",
                message="请求流已结束，无法继续处理后台交互",
            )
        await self._idle.wait()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_task_timeout()
        pending = list(self._pending.values())
        self._pending.clear()
        for item in pending:
            if item.timeout_task is not None:
                item.timeout_task.cancel()
        if self._session is not None and self._running:
            with suppress(Exception):
                await self._session.cancel()
            self._running = False
        if self._event_task is not None:
            self._event_task.cancel()
            await asyncio.gather(self._event_task, return_exceptions=True)
        if self._session is not None:
            await self._session.close()
