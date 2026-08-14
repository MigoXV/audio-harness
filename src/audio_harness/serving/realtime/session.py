from __future__ import annotations

import asyncio
import copy
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal
from typing import cast
from uuid import uuid4

from openai.types.realtime import (
    ConversationItemCreateEvent,
    ConversationItemDeleteEvent,
    ConversationItemRetrieveEvent,
    ConversationItemTruncateEvent,
    ConversationItemInputAudioTranscriptionCompletedEvent,
    ConversationItemInputAudioTranscriptionDeltaEvent,
    ConversationItemInputAudioTranscriptionFailedEvent,
    InputAudioBufferAppendEvent,
    InputAudioBufferClearEvent,
    InputAudioBufferClearedEvent,
    InputAudioBufferCommittedEvent,
    InputAudioBufferCommitEvent,
    InputAudioBufferSpeechStartedEvent,
    InputAudioBufferSpeechStoppedEvent,
    OutputAudioBufferClearEvent,
    RealtimeClientEvent,
    RealtimeConversationItemUserMessage,
    RealtimeError,
    RealtimeErrorEvent,
    RealtimeTranscriptionSessionCreateRequest,
    SessionCreatedEvent,
    SessionUpdateEvent,
    SessionUpdatedEvent,
    ResponseCancelEvent,
    ResponseCreateEvent,
)
from openai.types.realtime.conversation_item_input_audio_transcription_completed_event import (
    UsageTranscriptTextUsageDuration,
)
from openai.types.realtime.conversation_item_input_audio_transcription_failed_event import (
    Error as TranscriptionError,
)

from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.limits import ResizableConcurrencyLimiter
from audio_harness.engines.pool import InferencerPool
from audio_harness.serving.realtime.conversation import (
    ConversationMutationError,
)
from audio_harness.serving.realtime.agent import (
    AudioClearer,
    AudioFinisher,
    AudioSender,
    RealtimeAgentRuntime,
    VoiceResolver,
)
from audio_harness.serving.realtime.tools import ToolRegistry
from audio_harness.sessions import (
    AudioWorkflowSession,
    AudioWorkflowSessionReservation,
    AudioWorkflowSessionService,
)
from audio_harness.realtime_routes.service import RealtimeTarget
from audio_harness.voice_agents.service import ActiveVoiceAgent
from audio_harness.workflows.settings import WorkflowSettingsStore
from audio_harness.workflows.models import PhysicalVadNode
from audio_harness.workflows.service import ActiveAudioWorkflow
from audio_harness.workflows.events import SpeechStarted, SpeechStopped, VectorSearchResult

logger = logging.getLogger(__name__)

INPUT_SAMPLE_RATE = 24_000
ENGINE_SAMPLE_RATE = 16_000

EventSender = Callable[[Any], Awaitable[None]]
AudioEventHandler = Callable[[InputAudioBufferAppendEvent], Awaitable[None]]
_DEFAULT_TURN_DETECTION = object()


def event_id() -> str:
    return f"event_{uuid4().hex}"


def item_id() -> str:
    return f"item_{uuid4().hex}"


def session_id() -> str:
    return f"sess_{uuid4().hex}"


def default_transcription_session(
    *,
    model: str,
    language: str,
    prefix_padding_ms: int,
    session_identifier: str | None = None,
    audio_format: dict[str, Any] | None = None,
    turn_detection: dict[str, Any] | None | object = _DEFAULT_TURN_DETECTION,
) -> RealtimeTranscriptionSessionCreateRequest:
    if turn_detection is _DEFAULT_TURN_DETECTION:
        effective_turn_detection: dict[str, Any] | None = {
            "type": "server_vad",
            "prefix_padding_ms": prefix_padding_ms,
            "create_response": False,
        }
    elif turn_detection is None:
        effective_turn_detection = None
    else:
        effective_turn_detection = dict(cast(dict[str, Any], turn_detection))
        if effective_turn_detection.get("type") == "server_vad":
            effective_turn_detection.setdefault(
                "prefix_padding_ms",
                prefix_padding_ms,
            )
            effective_turn_detection["create_response"] = False
    payload = {
        "type": "transcription",
        "id": session_identifier,
        "object": "realtime.transcription_session",
        "audio": {
            "input": {
                "format": audio_format
                or {"type": "audio/pcm", "rate": INPUT_SAMPLE_RATE},
                "transcription": {
                    "model": model,
                    "language": language,
                },
                "turn_detection": effective_turn_detection,
            }
        },
    }
    return RealtimeTranscriptionSessionCreateRequest.model_validate(payload)


def session_values(
    session: RealtimeTranscriptionSessionCreateRequest,
    *,
    default_model: str,
    default_language: str,
) -> tuple[str, str]:
    payload = session.model_dump(
        mode="json",
        exclude_none=False,
        exclude_unset=True,
    )
    if payload.get("type") != "transcription":
        raise ValueError("第一版只支持 transcription session")
    audio_input = (payload.get("audio") or {}).get("input") or {}
    _transcription_audio_settings(session)
    transcription = audio_input.get("transcription") or {}
    requested_model = transcription.get("model")
    model = (
        requested_model.strip()
        if isinstance(requested_model, str) and requested_model.strip()
        else default_model
    )
    language = transcription.get("language")
    if not isinstance(language, str) or not language.strip():
        languages = transcription.get("languages")
        language = (
            languages[0]
            if isinstance(languages, list)
            and languages
            and isinstance(languages[0], str)
            else default_language
        )
    return model, language.strip()


def _transcription_audio_settings(
    session: RealtimeTranscriptionSessionCreateRequest,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload = session.model_dump(
        mode="json",
        exclude_none=False,
        exclude_unset=True,
    )
    audio_input = (payload.get("audio") or {}).get("input") or {}
    audio_format = audio_input.get("format") or {
        "type": "audio/pcm",
        "rate": INPUT_SAMPLE_RATE,
    }
    format_type = audio_format.get("type")
    if format_type == "audio/pcm":
        if audio_format.get("rate", INPUT_SAMPLE_RATE) != INPUT_SAMPLE_RATE:
            raise ValueError("audio/pcm 只支持 24 kHz")
        audio_format = {"type": "audio/pcm", "rate": INPUT_SAMPLE_RATE}
    else:
        raise ValueError(f"不支持的输入音频格式: {format_type}")
    turn_detection = audio_input.get("turn_detection", {"type": "server_vad"})
    if turn_detection is not None and (
        not isinstance(turn_detection, dict)
        or turn_detection.get("type") not in {"server_vad", "semantic_vad"}
    ):
        raise ValueError(
            "turn_detection 只支持 server_vad、semantic_vad 或 null"
        )
    return audio_format, turn_detection


@dataclass
class _TranscriptItem:
    item_id: str
    start_sample: int
    end_sample: int
    sent_transcript: str = ""


def _join_interim_parts(parts: list[str]) -> str:
    result = ""
    for raw in parts:
        part = raw.strip()
        if not part:
            continue
        separator = (
            " "
            if result
            and result[-1].isascii()
            and result[-1].isalnum()
            and part[0].isascii()
            and part[0].isalnum()
            else ""
        )
        result += separator + part
    return result


class RealtimeTranscriptionSession:
    """OpenAI Realtime 转写语义，不感知 WebSocket 或 WebRTC。"""

    def __init__(
        self,
        *,
        send_event: EventSender,
        session_service: AudioWorkflowSessionService,
        workflow_settings: WorkflowSettingsStore,
        model: str,
        target: RealtimeTarget,
        initial_session: Any | None = None,
        reservation: AudioWorkflowSessionReservation | None = None,
        audio_event_handler: AudioEventHandler | None = None,
        diagnostics_enabled: bool = False,
        aha_version: str | None = None,
        inferencer_pool: InferencerPool | None = None,
        responses_limiter: ResizableConcurrencyLimiter | None = None,
        tts_limiter: ResizableConcurrencyLimiter | None = None,
        tool_registry: ToolRegistry | None = None,
        send_audio: AudioSender | None = None,
        clear_audio: AudioClearer | None = None,
        voice_resolver: VoiceResolver | None = None,
        finish_audio: AudioFinisher | None = None,
        transport: Literal["websocket", "webrtc"] = "websocket",
        session_identifier: str | None = None,
    ) -> None:
        self._send_event = send_event
        self._session_service = session_service
        self._workflow_config = workflow_settings.current().config
        self._workflow = (
            target.audio_workflow
            if isinstance(target, ActiveVoiceAgent)
            else target
        )
        speech_started_binding = next(
            (
                output
                for output in self._workflow.plan.definition.outputs
                if output.name == "speech_started"
            ),
            None,
        )
        physical_node = (
            self._workflow.plan.nodes.get(
                speech_started_binding.source.node_id
            )
            if speech_started_binding is not None
            else None
        )
        if physical_node is None or not isinstance(
            physical_node.definition,
            PhysicalVadNode,
        ):
            raise RuntimeError("所选工作流缺少物理 VAD 配置")
        self._physical_vad_config = cast(
            PhysicalVadNode,
            physical_node.definition,
        ).config
        self._reservation = reservation
        self._audio_event_handler = audio_event_handler
        self._aha_version = aha_version
        self._transport = transport
        self.session_id = session_identifier or session_id()
        self.model = model
        self.language = self._workflow_config.default_language_code
        self._input_audio_format: dict[str, Any] = {
            "type": "audio/pcm",
            "rate": INPUT_SAMPLE_RATE,
        }
        self._turn_detection: dict[str, Any] | None = {"type": "server_vad"}
        if (
            initial_session is not None
            and isinstance(target, ActiveAudioWorkflow)
        ):
            self.model, self.language = session_values(
                initial_session,
                default_model=self.model,
                default_language=self.language,
            )
            (
                self._input_audio_format,
                self._turn_detection,
            ) = _transcription_audio_settings(initial_session)
        self._agent: RealtimeAgentRuntime | None = None
        if isinstance(target, ActiveVoiceAgent):
            if inferencer_pool is None:
                raise RuntimeError("语音智能体会话缺少引擎路由器")
            self._agent = RealtimeAgentRuntime(
                profile=target,
                inferencer_pool=inferencer_pool,
                send_event=self._send_event,
                send_audio=send_audio,
                clear_audio=clear_audio,
                responses_limiter=responses_limiter,
                tts_limiter=tts_limiter,
                tool_registry=tool_registry or ToolRegistry(),
                voice_resolver=voice_resolver,
                finish_audio=finish_audio,
            )
            if initial_session is not None:
                self._agent.update_options(
                    initial_session.model_dump(
                        mode="json",
                        exclude_none=False,
                        exclude_unset=True,
                    )
                )
            self.session = self._agent.session_document(model=self.model)
        else:
            self.session = default_transcription_session(
                model=self.model,
                language=self.language,
                session_identifier=self.session_id,
                prefix_padding_ms=round(
                    self._physical_vad_config.speech_pre_roll_seconds * 1000
                ),
                audio_format=self._input_audio_format,
                turn_detection=self._turn_detection,
            )
        self._transcription: AudioWorkflowSession | None = None
        self._session_context: Any | None = None
        self._workflow_task: asyncio.Task[None] | None = None
        self._active_item_id: str | None = None
        self._pending_items: deque[_TranscriptItem] = deque()
        self._interim_segments: dict[tuple[int, int], str] = {}
        self._interim_item_id: str | None = None
        self._interim_revision = 0
        self._interim_sent_transcript = ""
        self._interim_was_sent = False
        self._interim_suppressed_item_id: str | None = None
        self._previous_item_id: str | None = None
        self._manual_audio_buffer = bytearray()
        self._audio_started = False
        self._closed = False

    @property
    def audio_started(self) -> bool:
        return self._audio_started

    @property
    def input_audio_format(self) -> dict[str, Any]:
        if self._agent is not None:
            return copy.deepcopy(self._agent.options.input_audio_format)
        return copy.deepcopy(self._input_audio_format)

    @property
    def _manual_commit_enabled(self) -> bool:
        turn_detection = (
            self._agent.options.turn_detection
            if self._agent is not None
            else self._turn_detection
        )
        return turn_detection is None

    async def start(self) -> None:
        if self._agent is not None:
            await self._send_event(
                {
                    "event_id": event_id(),
                    "session": self.session,
                    "type": "session.created",
                }
            )
        else:
            await self._send_event(
                SessionCreatedEvent(
                    event_id=event_id(),
                    session=self.session,
                    type="session.created",
                )
            )

    async def handle_client_event(self, event: RealtimeClientEvent) -> None:
        if isinstance(event, SessionUpdateEvent):
            await self._handle_session_update(event)
            return
        if isinstance(event, InputAudioBufferAppendEvent):
            if self._audio_event_handler is None:
                await self.send_error(
                    message="WebRTC 音频必须通过 RTP 音轨发送",
                    code="unsupported_audio_event",
                    client_event_id=event.event_id,
                    param="type",
                )
            else:
                await self._audio_event_handler(event)
            return
        if isinstance(event, InputAudioBufferClearEvent):
            await self.reset()
            await self._send_event(
                InputAudioBufferClearedEvent(
                    event_id=event_id(),
                    type="input_audio_buffer.cleared",
                )
            )
            return
        if isinstance(event, ResponseCancelEvent):
            if self._agent is None:
                await self.send_error(
                    message="转写工作流没有可取消的响应",
                    code="unsupported_event",
                    client_event_id=event.event_id,
                )
            else:
                try:
                    await self._agent.interrupt(
                        reason="client_cancelled",
                        response_id=event.response_id,
                    )
                except ValueError as exc:
                    await self.send_error(
                        message=str(exc),
                        code="response_cancel_not_active",
                        client_event_id=event.event_id,
                        param="response_id",
                    )
            return
        if isinstance(event, ResponseCreateEvent):
            if self._agent is None:
                await self.send_error(
                    message="转写工作流不支持 response.create",
                    code="unsupported_event",
                    client_event_id=event.event_id,
                )
            else:
                try:
                    await self._agent.create_response(event.response)
                except ValueError as exc:
                    await self.send_error(
                        message=str(exc),
                        code="invalid_response",
                        client_event_id=event.event_id,
                        param="response",
                    )
            return
        if isinstance(event, ConversationItemCreateEvent):
            if self._agent is None:
                await self.send_error(
                    message="转写工作流不支持 conversation.item.create",
                    code="unsupported_event",
                    client_event_id=event.event_id,
                )
                return
            item = event.item
            try:
                add_to_conversation = True
                if item.type == "function_call_output":
                    add_to_conversation = await self._agent.submit_function_output(
                        item.call_id,
                        item.output,
                    )
                elif item.type == "mcp_approval_response":
                    add_to_conversation = await self._agent.submit_mcp_approval(
                        item.approval_request_id,
                        item.approve,
                    )
                if not add_to_conversation:
                    return
                created, previous = self._agent.create_conversation_item(
                    item,
                    previous_item_id=event.previous_item_id,
                )
                await self._send_event(
                    {
                        "type": "conversation.item.added",
                        "event_id": event_id(),
                        "previous_item_id": previous,
                        "item": created,
                    }
                )
                await self._send_event(
                    {
                        "type": "conversation.item.done",
                        "event_id": event_id(),
                        "previous_item_id": None,
                        "item": created,
                    }
                )
            except (ConversationMutationError, ValueError) as exc:
                await self.send_error(
                    message=str(exc),
                    code=getattr(
                        exc,
                        "code",
                        "invalid_conversation_item",
                    ),
                    client_event_id=event.event_id,
                    param=getattr(exc, "param", "item"),
                )
            return
        if isinstance(event, ConversationItemRetrieveEvent):
            if self._agent is None:
                await self.send_error(
                    message="转写工作流不支持 conversation item 操作",
                    code="unsupported_event",
                    client_event_id=event.event_id,
                )
                return
            try:
                item = self._agent.retrieve_conversation_item(event.item_id)
                await self._send_event(
                    {
                        "type": "conversation.item.retrieved",
                        "event_id": event_id(),
                        "item": item,
                    }
                )
            except ConversationMutationError as exc:
                await self.send_error(
                    message=str(exc),
                    code=exc.code,
                    client_event_id=event.event_id,
                    param=exc.param,
                )
            return
        if isinstance(event, ConversationItemDeleteEvent):
            if self._agent is None:
                await self.send_error(
                    message="转写工作流不支持 conversation item 操作",
                    code="unsupported_event",
                    client_event_id=event.event_id,
                )
                return
            try:
                self._agent.delete_conversation_item(event.item_id)
                await self._send_event(
                    {
                        "type": "conversation.item.deleted",
                        "event_id": event_id(),
                        "item_id": event.item_id,
                    }
                )
            except ConversationMutationError as exc:
                await self.send_error(
                    message=str(exc),
                    code=exc.code,
                    client_event_id=event.event_id,
                    param=exc.param,
                )
            return
        if isinstance(event, ConversationItemTruncateEvent):
            if self._agent is None:
                await self.send_error(
                    message="转写工作流不支持 conversation item 操作",
                    code="unsupported_event",
                    client_event_id=event.event_id,
                )
                return
            try:
                self._agent.truncate_conversation_item(
                    event.item_id,
                    content_index=event.content_index,
                    audio_end_ms=event.audio_end_ms,
                )
                await self._send_event(
                    {
                        "type": "conversation.item.truncated",
                        "event_id": event_id(),
                        "item_id": event.item_id,
                        "content_index": event.content_index,
                        "audio_end_ms": event.audio_end_ms,
                    }
                )
            except ConversationMutationError as exc:
                await self.send_error(
                    message=str(exc),
                    code=exc.code,
                    client_event_id=event.event_id,
                    param=exc.param,
                )
            return
        if isinstance(event, OutputAudioBufferClearEvent):
            if self._transport != "webrtc" or self._agent is None:
                await self.send_error(
                    message=(
                        "output_audio_buffer.clear 仅支持 WebRTC 或 SIP 连接"
                    ),
                    code="invalid_value",
                    client_event_id=event.event_id,
                    param="type",
                )
                return
            try:
                response_id = await self._agent.clear_output_audio()
            except ValueError as exc:
                await self.send_error(
                    message=str(exc),
                    code="invalid_value",
                    client_event_id=event.event_id,
                    param="type",
                )
                return
            await self._send_event(
                {
                    "type": "output_audio_buffer.cleared",
                    "event_id": event_id(),
                    "response_id": response_id,
                }
            )
            return
        if isinstance(event, InputAudioBufferCommitEvent):
            if not self._audio_started:
                await self.send_error(
                    message="输入音频缓冲区为空",
                    code="input_audio_buffer_commit_empty",
                    client_event_id=event.event_id,
                    param="type",
                )
                return
            if self._manual_commit_enabled:
                await self._commit_manual_audio()
            else:
                await self._finish_workflow(send_results=True)
            self._audio_started = False
            return
        await self.send_error(
            message=f"第一版不支持客户端事件: {event.type}",
            code="unsupported_event",
            client_event_id=getattr(event, "event_id", None),
            param="type",
        )

    async def append_engine_audio(self, audio: bytes) -> None:
        if self._closed:
            raise RuntimeError("Realtime 转写会话已经关闭")
        if len(audio) % 2:
            raise ValueError("PCM S16LE 音频字节数必须为偶数")
        if not audio:
            return
        self._audio_started = True
        if self._manual_commit_enabled:
            self._manual_audio_buffer.extend(audio)
            return
        if self._workflow_task is None:
            await self._start_workflow()
        assert self._transcription is not None
        await self._transcription.append(audio)

    async def finish_audio(self) -> None:
        await self._finish_workflow(send_results=True)

    async def send_error(
        self,
        *,
        message: str,
        code: str,
        client_event_id: str | None = None,
        param: str | None = None,
    ) -> None:
        await self._send_event(
            RealtimeErrorEvent(
                error=RealtimeError(
                    message=message,
                    type="invalid_request_error",
                    code=code,
                    event_id=client_event_id,
                    param=param,
                ),
                event_id=event_id(),
                type="error",
            )
        )

    async def reset(self) -> None:
        try:
            await self._finish_workflow(send_results=False)
        finally:
            await self._clear_interim("input_audio_buffer_cleared")
            self._active_item_id = None
            self._pending_items.clear()
            self._manual_audio_buffer.clear()
            self._audio_started = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._agent is not None:
                await self._agent.close()
        finally:
            self._manual_audio_buffer.clear()
            await self._finish_workflow(send_results=False)

    async def _commit_manual_audio(self) -> None:
        audio = bytes(self._manual_audio_buffer)
        self._manual_audio_buffer.clear()
        committed_item = _TranscriptItem(
            item_id=item_id(),
            start_sample=0,
            end_sample=len(audio) // 2,
        )
        await self._emit_committed_item(committed_item)
        await self._start_workflow()
        assert self._transcription is not None
        await self._transcription.append(audio)
        await self._finish_workflow(send_results=True)
        self._active_item_id = None

        if self._pending_items and self._pending_items[0] is committed_item:
            await self._emit_completed_transcription(committed_item, "")
            self._pending_items.popleft()

    async def _handle_session_update(self, event: SessionUpdateEvent) -> None:
        if self._agent is not None:
            try:
                payload = event.session.model_dump(
                    mode="json",
                    exclude_none=False,
                    exclude_unset=True,
                )
                requested_model = payload.get("model")
                if (
                    requested_model is not None
                    and requested_model != self.model
                ):
                    raise ValueError("session model 在建连后不可修改")
                self._agent.update_options(payload)
                self.session = self._agent.session_document(model=self.model)
            except ValueError as exc:
                await self.send_error(
                    message=str(exc),
                    code="invalid_session",
                    client_event_id=event.event_id,
                    param="session",
                )
                return
            await self._send_event(
                {
                    "event_id": event_id(),
                    "session": self.session,
                    "type": "session.updated",
                }
            )
            return
        try:
            requested = RealtimeTranscriptionSessionCreateRequest.model_validate(
                event.session.model_dump(
                    mode="json",
                    exclude_none=False,
                    exclude_unset=True,
                )
            )
            model, language = session_values(
                requested,
                default_model=self.model,
                default_language=self.language,
            )
            input_audio_format, turn_detection = _transcription_audio_settings(
                requested
            )
        except ValueError as exc:
            await self.send_error(
                message=str(exc),
                code="invalid_session",
                client_event_id=event.event_id,
                param="session",
            )
            return
        if self._audio_started and (
            model != self.model or input_audio_format != self._input_audio_format
        ):
            await self.send_error(
                message=(
                    "收到音频后必须先清空或显式提交输入缓冲区，"
                    "才能修改音频格式或转写模型"
                ),
                code="session_update_after_audio",
                client_event_id=event.event_id,
                param="session",
            )
            return
        self.model = model
        self.language = language
        self._input_audio_format = input_audio_format
        self._turn_detection = turn_detection
        self.session = default_transcription_session(
            model=self.model,
            language=self.language,
            session_identifier=self.session_id,
            prefix_padding_ms=round(
                self._physical_vad_config.speech_pre_roll_seconds * 1000
            ),
            audio_format=self._input_audio_format,
            turn_detection=self._turn_detection,
        )
        await self._send_event(
            SessionUpdatedEvent(
                event_id=event_id(),
                session=self.session,
                type="session.updated",
            )
        )

    async def _start_workflow(self) -> None:
        options = AsrInferencer.Options(
            sample_rate=ENGINE_SAMPLE_RATE,
            language_code=self.language,
            enable_automatic_punctuation=True,
        )
        if self._reservation is not None:
            self._transcription = await self._reservation.create(
                options=options,
                include_vad_events=True,
                include_vad_progress=self._aha_version == "v1",
                include_interim_transcripts=(
                    self._aha_version == "v1"
                    and not self._manual_commit_enabled
                ),
                include_vector_search=self._aha_version == "v1",
                workflow=self._workflow,
            )
        else:
            self._session_context = self._session_service.open(
                options,
                include_vad_events=True,
                include_vad_progress=self._aha_version == "v1",
                include_interim_transcripts=(
                    self._aha_version == "v1"
                    and not self._manual_commit_enabled
                ),
                include_vector_search=self._aha_version == "v1",
                workflow=self._workflow,
            )
            self._transcription = await self._session_context.__aenter__()
        self._workflow_task = asyncio.create_task(
            self._run_workflow(),
            name="audio-harness-realtime-workflow",
        )

    async def _run_workflow(self) -> None:
        try:
            assert self._transcription is not None
            async for output in self._transcription.outputs():
                await self._handle_workflow_event(output.name, output.payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Realtime 工作流失败")
            if self._pending_items:
                item = self._pending_items[0]
                with suppress(Exception):
                    await self._send_event(
                        ConversationItemInputAudioTranscriptionFailedEvent(
                            content_index=0,
                            error=TranscriptionError(
                                code="transcription_failed",
                                message=str(exc),
                                param=None,
                                type="server_error",
                            ),
                            event_id=event_id(),
                            item_id=item.item_id,
                            type=(
                                "conversation.item."
                                "input_audio_transcription.failed"
                            ),
                        )
                    )
            with suppress(Exception):
                await self.send_error(
                    message=str(exc),
                    code="workflow_failed",
                )

    async def _handle_workflow_event(self, output_name: str, event: Any) -> None:
        if output_name == "vector_search" and isinstance(event, VectorSearchResult):
            if self._aha_version != "v1":
                return
            current_item_id = self._active_item_id or self._interim_item_id
            if current_item_id is None and self._pending_items:
                current_item_id = self._pending_items[0].item_id
            if current_item_id is None:
                logger.warning("向量搜索结果缺少关联 Realtime item")
                return
            common = {
                "event_id": event_id(), "item_id": current_item_id,
                "audio_start_ms": round(event.start_sample * 1000 / event.sample_rate),
                "audio_end_ms": round(event.end_sample * 1000 / event.sample_rate),
                "collection_name": event.collection_name,
                "vector_name": event.vector_name,
            }
            if event.error_code is not None:
                await self._send_event({"type": "x_aha.vector.search.failed", **common, "error": {"code": event.error_code, "message": event.error_message or "向量搜索失败"}})
            else:
                payload = {"type": "x_aha.vector.search.completed", **common, "matches": list(event.matches), "latency_ms": round(event.latency_ms, 3)}
                if event.embedding is not None:
                    payload["embedding"] = list(event.embedding)
                await self._send_event(payload)
            return
        if output_name == "interim_transcript":
            if (
                self._aha_version == "v1"
                and not self._manual_commit_enabled
                and isinstance(event, AsrInferencer.Result)
            ):
                await self._handle_interim_transcript(event)
            return
        if output_name == "turn_progress":
            if (
                self._aha_version == "v1"
                and not self._manual_commit_enabled
                and isinstance(event, AsrInferencer.Result)
                and not event.turn_completed
            ):
                if self._active_item_id is None:
                    logger.warning("语义 VAD 进度缺少活动 Realtime item")
                    return
                await self._send_event(
                    {
                        "type": "x_aha.vad.physical_speech_stopped",
                        "event_id": event_id(),
                        "item_id": self._active_item_id,
                        "audio_start_ms": round(
                            event.start_sample * 1000 / event.sample_rate
                        ),
                        "audio_end_ms": round(
                            event.end_sample * 1000 / event.sample_rate
                        ),
                        "turn_completed": False,
                    }
                )
            return
        if isinstance(event, SpeechStarted):
            if self._active_item_id is not None:
                return
            if self._interim_item_id is not None:
                await self._clear_interim("superseded")
            interrupt_response = (
                self._agent is not None
                and self._agent.active_response_id is not None
                and self._agent.options.turn_detection is not None
                and self._agent.options.turn_detection.get(
                    "interrupt_response",
                    True,
                )
            )
            self._active_item_id = item_id()
            self._interim_item_id = self._active_item_id
            self._interim_suppressed_item_id = None
            if self._manual_commit_enabled:
                return
            await self._send_event(
                InputAudioBufferSpeechStartedEvent(
                    audio_start_ms=round(
                        event.start_sample * 1000 / event.sample_rate
                    ),
                    event_id=event_id(),
                    item_id=self._active_item_id,
                    type="input_audio_buffer.speech_started",
                )
            )
            if interrupt_response:
                assert self._agent is not None
                await self._agent.interrupt(reason="turn_detected")
            await self._emit_interim_snapshot(event.sample_rate)
            return
        if isinstance(event, SpeechStopped):
            current_item_id = self._active_item_id or item_id()
            self._active_item_id = None
            if self._manual_commit_enabled:
                return
            item = _TranscriptItem(
                item_id=current_item_id,
                start_sample=event.start_sample,
                end_sample=event.end_sample,
            )
            self._pending_items.append(item)
            await self._send_event(
                InputAudioBufferSpeechStoppedEvent(
                    audio_end_ms=round(
                        event.end_sample * 1000 / event.sample_rate
                    ),
                    event_id=event_id(),
                    item_id=current_item_id,
                    type="input_audio_buffer.speech_stopped",
                )
            )
            await self._emit_committed_item(item, append_pending=False)
            return
        if not isinstance(event, AsrInferencer.Result):
            return
        if not self._pending_items:
            await self._emit_committed_item(
                _TranscriptItem(
                    item_id=item_id(),
                    start_sample=event.start_sample,
                    end_sample=event.end_sample,
                ),
            )
        item = self._pending_items[0]
        if self._interim_suppressed_item_id != item.item_id:
            await self._clear_interim(
                "primary_transcript_started",
                suppress_item_id=item.item_id,
            )
        delta = (
            event.transcript[len(item.sent_transcript) :]
            if event.transcript.startswith(item.sent_transcript)
            else ""
        )
        if delta:
            item.sent_transcript = event.transcript
            await self._send_event(
                ConversationItemInputAudioTranscriptionDeltaEvent(
                    event_id=event_id(),
                    item_id=item.item_id,
                    type=(
                        "conversation.item."
                        "input_audio_transcription.delta"
                    ),
                    content_index=0,
                    delta=delta,
                    logprobs=None,
                )
            )
        if event.is_final:
            await self._emit_completed_transcription(
                item,
                event.transcript,
                sample_rate=event.sample_rate,
            )
            self._pending_items.popleft()
            if self._agent is not None and event.transcript.strip():
                self._agent.add_transcribed_audio(
                    item.item_id,
                    event.transcript,
                )
                turn_detection = self._agent.options.turn_detection
                if turn_detection is not None and turn_detection.get(
                    "create_response",
                    True,
                ):
                    await self._agent.respond(
                        event.transcript,
                        input_item_id=item.item_id,
                    )

    async def _handle_interim_transcript(
        self,
        event: AsrInferencer.Result,
    ) -> None:
        current_item_id = self._active_item_id or self._interim_item_id
        if current_item_id is None and self._pending_items:
            current_item_id = self._pending_items[0].item_id
        if current_item_id == self._interim_suppressed_item_id:
            return
        key = (event.start_sample, event.end_sample)
        self._interim_segments[key] = event.transcript
        if current_item_id is None:
            return
        self._interim_item_id = current_item_id
        await self._emit_interim_snapshot(event.sample_rate)

    async def _emit_interim_snapshot(self, sample_rate: int) -> None:
        if self._interim_item_id is None or not self._interim_segments:
            return
        ordered = sorted(self._interim_segments.items())
        transcript = _join_interim_parts([text for _, text in ordered])
        if transcript == self._interim_sent_transcript:
            return
        self._interim_revision += 1
        self._interim_sent_transcript = transcript
        self._interim_was_sent = True
        await self._send_event(
            {
                "type": "x_aha.input_audio_transcription.interim",
                "event_id": event_id(),
                "item_id": self._interim_item_id,
                "content_index": 0,
                "revision": self._interim_revision,
                "transcript": transcript,
                "audio_start_ms": round(
                    ordered[0][0][0] * 1000 / sample_rate
                ),
                "audio_end_ms": round(
                    max(key[1] for key, _ in ordered) * 1000 / sample_rate
                ),
            }
        )

    async def _clear_interim(
        self,
        reason: str,
        *,
        suppress_item_id: str | None = None,
    ) -> None:
        current_item_id = self._interim_item_id
        if self._interim_was_sent and current_item_id is not None:
            await self._send_event(
                {
                    "type": "x_aha.input_audio_transcription.interim_cleared",
                    "event_id": event_id(),
                    "item_id": current_item_id,
                    "reason": reason,
                }
            )
        self._interim_segments.clear()
        self._interim_item_id = None
        self._interim_revision = 0
        self._interim_sent_transcript = ""
        self._interim_was_sent = False
        self._interim_suppressed_item_id = suppress_item_id

    async def _emit_committed_item(
        self,
        item: _TranscriptItem,
        *,
        append_pending: bool = True,
    ) -> None:
        if append_pending:
            self._pending_items.append(item)
        previous_item_id = self._previous_item_id
        await self._send_event(
            InputAudioBufferCommittedEvent(
                event_id=event_id(),
                item_id=item.item_id,
                previous_item_id=previous_item_id,
                type="input_audio_buffer.committed",
            )
        )
        realtime_item = RealtimeConversationItemUserMessage(
            content=[{"type": "input_audio"}],
            id=item.item_id,
            object="realtime.item",
            role="user",
            status="completed",
            type="message",
        ).model_dump(mode="json", exclude_none=True)
        await self._send_event(
            {
                "event_id": event_id(),
                "item": realtime_item,
                "previous_item_id": previous_item_id,
                "type": "conversation.item.added",
            }
        )
        await self._send_event(
            {
                "event_id": event_id(),
                "item": realtime_item,
                "previous_item_id": None,
                "type": "conversation.item.done",
            }
        )
        self._previous_item_id = item.item_id

    async def _emit_completed_transcription(
        self,
        item: _TranscriptItem,
        transcript: str,
        *,
        sample_rate: int = ENGINE_SAMPLE_RATE,
    ) -> None:
        await self._send_event(
            ConversationItemInputAudioTranscriptionCompletedEvent(
                content_index=0,
                event_id=event_id(),
                item_id=item.item_id,
                transcript=transcript,
                type=(
                    "conversation.item.input_audio_transcription.completed"
                ),
                usage=UsageTranscriptTextUsageDuration(
                    seconds=(item.end_sample - item.start_sample) / sample_rate,
                    type="duration",
                ),
                languages=None,
                logprobs=None,
            )
        )

    async def _finish_workflow(self, *, send_results: bool) -> None:
        task = self._workflow_task
        if task is None:
            return
        failure: BaseException | None = None
        try:
            if self._transcription is not None:
                await self._transcription.finish()
            if send_results:
                await task
        except BaseException as exc:
            failure = exc
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            try:
                if self._session_context is not None:
                    await self._session_context.__aexit__(None, None, None)
                elif self._reservation is not None:
                    await self._reservation.close_session()
            except BaseException as exc:
                if failure is None:
                    failure = exc
                else:
                    logger.error(
                        "Realtime 工作流失败后的资源释放也发生异常",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
            finally:
                self._workflow_task = None
                self._session_context = None
                self._transcription = None
        if failure is not None:
            raise failure
