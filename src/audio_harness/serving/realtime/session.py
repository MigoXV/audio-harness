from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from openai.types.realtime import (
    ConversationItemCreatedEvent,
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
    RealtimeClientEvent,
    RealtimeConversationItemUserMessage,
    RealtimeError,
    RealtimeErrorEvent,
    RealtimeTranscriptionSessionCreateRequest,
    SessionCreatedEvent,
    SessionUpdateEvent,
    SessionUpdatedEvent,
)
from openai.types.realtime.conversation_item_input_audio_transcription_completed_event import (
    UsageTranscriptTextUsageDuration,
)
from openai.types.realtime.conversation_item_input_audio_transcription_failed_event import (
    Error as TranscriptionError,
)

from audio_harness.domain import (
    RecognitionOptions,
    SpeechStarted,
    SpeechStopped,
    TranscriptionResult,
    VadUpdate,
)
from audio_harness.sessions import (
    TranscriptionSession,
    TranscriptionSessionReservation,
    TranscriptionSessionService,
)
from audio_harness.workflow_settings import WorkflowSettingsStore

logger = logging.getLogger(__name__)

INPUT_SAMPLE_RATE = 24_000
ENGINE_SAMPLE_RATE = 16_000

EventSender = Callable[[Any], Awaitable[None]]
AudioEventHandler = Callable[[InputAudioBufferAppendEvent], Awaitable[None]]


def event_id() -> str:
    return f"event_{uuid4().hex}"


def item_id() -> str:
    return f"item_{uuid4().hex}"


def default_transcription_session(
    *,
    model: str,
    language: str,
    prefix_padding_ms: int,
) -> RealtimeTranscriptionSessionCreateRequest:
    return RealtimeTranscriptionSessionCreateRequest.model_validate(
        {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": INPUT_SAMPLE_RATE,
                    },
                    "transcription": {
                        "model": model,
                        "language": language,
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "prefix_padding_ms": prefix_padding_ms,
                        "create_response": False,
                    },
                }
            },
        }
    )


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
    audio_format = audio_input.get("format")
    if audio_format is not None and (
        audio_format.get("type") != "audio/pcm"
        or audio_format.get("rate", INPUT_SAMPLE_RATE) != INPUT_SAMPLE_RATE
    ):
        raise ValueError("第一版只支持 24 kHz audio/pcm")
    turn_detection = audio_input.get("turn_detection", "__missing__")
    if turn_detection != "__missing__" and (
        not isinstance(turn_detection, dict)
        or turn_detection.get("type") != "server_vad"
    ):
        raise ValueError("第一版只支持 server_vad 自动分段")
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


@dataclass
class _TranscriptItem:
    item_id: str
    start_sample: int
    end_sample: int
    sent_transcript: str = ""


class RealtimeTranscriptionSession:
    """OpenAI Realtime 转写语义，不感知 WebSocket 或 WebRTC。"""

    def __init__(
        self,
        *,
        send_event: EventSender,
        session_service: TranscriptionSessionService,
        workflow_settings: WorkflowSettingsStore,
        model: str,
        initial_session: RealtimeTranscriptionSessionCreateRequest | None = None,
        reservation: TranscriptionSessionReservation | None = None,
        audio_event_handler: AudioEventHandler | None = None,
        diagnostics_enabled: bool = False,
    ) -> None:
        self._send_event = send_event
        self._session_service = session_service
        self._workflow_config = workflow_settings.current().config
        self._reservation = reservation
        self._audio_event_handler = audio_event_handler
        self._diagnostics_enabled = diagnostics_enabled
        self.model = model
        self.language = self._workflow_config.default_language_code
        if initial_session is not None:
            self.model, self.language = session_values(
                initial_session,
                default_model=self.model,
                default_language=self.language,
            )
        self.session = default_transcription_session(
            model=self.model,
            language=self.language,
            prefix_padding_ms=round(
                self._workflow_config.speech_pre_roll_seconds * 1000
            ),
        )
        self._transcription: TranscriptionSession | None = None
        self._session_context: Any | None = None
        self._workflow_task: asyncio.Task[None] | None = None
        self._active_item_id: str | None = None
        self._pending_items: deque[_TranscriptItem] = deque()
        self._previous_item_id: str | None = None
        self._audio_started = False
        self._closed = False

    @property
    def audio_started(self) -> bool:
        return self._audio_started

    async def start(self) -> None:
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
        if isinstance(event, InputAudioBufferCommitEvent):
            await self.send_error(
                message="第一版仅支持 server_vad 自动提交",
                code="unsupported_manual_commit",
                client_event_id=event.event_id,
                param="type",
            )
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
        if self._workflow_task is None:
            await self._start_workflow()
        assert self._transcription is not None
        self._audio_started = True
        await self._transcription.append(audio)

    async def finish_audio(self) -> None:
        if self._transcription is not None:
            await self._transcription.finish()
        if self._workflow_task is not None:
            await self._workflow_task

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
        await self._finish_workflow(send_results=False)
        self._active_item_id = None
        self._pending_items.clear()
        self._audio_started = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._finish_workflow(send_results=False)

    async def _handle_session_update(self, event: SessionUpdateEvent) -> None:
        if self._audio_started:
            await self.send_error(
                message="音频开始后不能修改转写会话",
                code="session_update_after_audio",
                client_event_id=event.event_id,
                param="session",
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
            self.model, self.language = session_values(
                requested,
                default_model=self.model,
                default_language=self.language,
            )
        except ValueError as exc:
            await self.send_error(
                message=str(exc),
                code="invalid_session",
                client_event_id=event.event_id,
                param="session",
            )
            return
        self.session = default_transcription_session(
            model=self.model,
            language=self.language,
            prefix_padding_ms=round(
                self._workflow_config.speech_pre_roll_seconds * 1000
            ),
        )
        await self._send_event(
            SessionUpdatedEvent(
                event_id=event_id(),
                session=self.session,
                type="session.updated",
            )
        )

    async def _start_workflow(self) -> None:
        options = RecognitionOptions(
            sample_rate=ENGINE_SAMPLE_RATE,
            language_code=self.language,
            enable_automatic_punctuation=True,
        )
        if self._reservation is not None:
            self._transcription = self._reservation.create(
                options=options,
                include_vad_events=True,
                include_vad_progress=self._diagnostics_enabled,
                use_semantic_vad=False,
            )
        else:
            self._session_context = self._session_service.open(
                options,
                include_vad_events=True,
                include_vad_progress=self._diagnostics_enabled,
                use_semantic_vad=False,
            )
            self._transcription = await self._session_context.__aenter__()
        self._workflow_task = asyncio.create_task(
            self._run_workflow(),
            name="audio-harness-realtime-workflow",
        )

    async def _run_workflow(self) -> None:
        try:
            assert self._transcription is not None
            async for event in self._transcription.events():
                await self._handle_workflow_event(event)
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

    async def _handle_workflow_event(self, event: Any) -> None:
        if isinstance(event, VadUpdate):
            await self._send_event(
                {
                    "type": "audio_harness.vad_progress",
                    "event_id": event_id(),
                    "processed_audio_ms": (
                        event.processed_samples * 1000 / ENGINE_SAMPLE_RATE
                    ),
                }
            )
            return
        if isinstance(event, SpeechStarted):
            self._active_item_id = item_id()
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
            return
        if isinstance(event, SpeechStopped):
            current_item_id = self._active_item_id or item_id()
            self._active_item_id = None
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
            await self._send_event(
                InputAudioBufferCommittedEvent(
                    event_id=event_id(),
                    item_id=current_item_id,
                    previous_item_id=self._previous_item_id,
                    type="input_audio_buffer.committed",
                )
            )
            await self._send_event(
                ConversationItemCreatedEvent(
                    event_id=event_id(),
                    item=RealtimeConversationItemUserMessage(
                        content=[{"type": "input_audio"}],
                        id=current_item_id,
                        object="realtime.item",
                        role="user",
                        status="in_progress",
                        type="message",
                    ),
                    previous_item_id=self._previous_item_id,
                    type="conversation.item.created",
                )
            )
            self._previous_item_id = current_item_id
            return
        if not isinstance(event, TranscriptionResult):
            return
        if not self._pending_items:
            self._pending_items.append(
                _TranscriptItem(
                    item_id=item_id(),
                    start_sample=event.start_sample,
                    end_sample=event.end_sample,
                )
            )
        item = self._pending_items[0]
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
            await self._send_event(
                ConversationItemInputAudioTranscriptionCompletedEvent(
                    content_index=0,
                    event_id=event_id(),
                    item_id=item.item_id,
                    transcript=event.transcript,
                    type=(
                        "conversation.item."
                        "input_audio_transcription.completed"
                    ),
                    usage=UsageTranscriptTextUsageDuration(
                        seconds=(
                            event.end_sample - event.start_sample
                        )
                        / event.sample_rate,
                        type="duration",
                    ),
                    languages=None,
                    logprobs=None,
                )
            )
            self._pending_items.popleft()

    async def _finish_workflow(self, *, send_results: bool) -> None:
        task = self._workflow_task
        if task is None:
            return
        if self._transcription is not None:
            await self._transcription.finish()
        if send_results:
            await task
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._session_context is not None:
            await self._session_context.__aexit__(None, None, None)
        elif self._reservation is not None:
            await self._reservation.close_session()
        self._workflow_task = None
        self._session_context = None
        self._transcription = None
