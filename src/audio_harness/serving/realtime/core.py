from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any
from pathlib import Path

import numpy as np
import soxr
from openai.types.realtime import (
    InputAudioBufferAppendEvent,
    InputAudioBufferClearEvent,
    InputAudioBufferCommitEvent,
    RealtimeError,
    RealtimeErrorEvent,
    SessionUpdateEvent,
)
from pydantic import ValidationError

from audio_harness.inferencers.limits import ResizableConcurrencyLimiter
from audio_harness.agent_backends.base import AgentBackendGateway
from audio_harness.errors import CapacityExceededError
from audio_harness.engines.pool import InferencerPool
from audio_harness.serving.model_catalog import (
    RealtimeModelCatalog,
    RealtimeModelNotFoundError,
    RealtimeTarget,
)
from audio_harness.serving.realtime.session import (
    ENGINE_SAMPLE_RATE,
    INPUT_SAMPLE_RATE,
    RealtimeTranscriptionSession,
    event_id,
    session_id,
    session_values,
)
from audio_harness.serving.realtime.agent import VoiceResolver
from audio_harness.serving.realtime.protocol import (
    AhaRealtimeTranscriptionSessionCreateRequest,
    AhaSessionCreatedEvent,
    AhaV1ServerEventBase,
)
from audio_harness.sessions.audio_workflow import AudioWorkflowSessionService
from audio_harness.serving.realtime.tools import ToolRegistry
from audio_harness.workflows.settings import WorkflowSettingsStore
from audio_harness.voice_agents.service import ActiveVoiceAgent

from .contracts import CLIENT_EVENTS, SERVER_EVENTS, event_payload

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RealtimeOptions:
    intent: str | None = None
    model: str | None = None
    diagnostics_enabled: bool = False
    aha_version: str | None = None


class RealtimeConnectionRejected(Exception):
    def __init__(
        self, code: str, message: str, *, event: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.event = event


class RealtimeConnectionMode(str, Enum):
    """Wire-level Realtime modes supported by the data-plane endpoint."""

    TRANSCRIPTION = "transcription"
    REALTIME = "realtime"


class _OnlinePcmResampler:
    def __init__(
        self,
        chunk_bytes: int,
        input_sample_rate: int = INPUT_SAMPLE_RATE,
    ) -> None:
        self.chunk_bytes = chunk_bytes
        if input_sample_rate not in {ENGINE_SAMPLE_RATE, INPUT_SAMPLE_RATE}:
            raise ValueError("输入采样率只支持 16 kHz 或 24 kHz")
        self.input_sample_rate = input_sample_rate
        self._resampler = (
            None
            if input_sample_rate == ENGINE_SAMPLE_RATE
            else soxr.ResampleStream(
                input_sample_rate,
                ENGINE_SAMPLE_RATE,
                1,
                dtype="int16",
                quality="HQ",
            )
        )
        self._buffer = bytearray()
        self._finished = False

    def _take_chunks(self, *, flush: bool) -> list[bytes]:
        chunks: list[bytes] = []
        while len(self._buffer) >= self.chunk_bytes:
            chunks.append(bytes(self._buffer[: self.chunk_bytes]))
            del self._buffer[: self.chunk_bytes]
        if flush and self._buffer:
            chunks.append(bytes(self._buffer))
            self._buffer.clear()
        return chunks

    def append(self, audio: bytes) -> list[bytes]:
        if self._finished:
            raise ValueError("音频输入已经结束")
        if not audio:
            return []
        if len(audio) % 2:
            raise ValueError("PCM S16LE 音频字节数必须为偶数")
        samples = np.frombuffer(audio, dtype="<i2")
        converted = (
            samples
            if self._resampler is None
            else self._resampler.resample_chunk(samples, last=False)
        )
        self._buffer.extend(converted.astype("<i2", copy=False).tobytes())
        return self._take_chunks(flush=False)

    def finish(self) -> list[bytes]:
        if self._finished:
            return []
        self._finished = True
        converted = (
            np.empty(0, dtype=np.int16)
            if self._resampler is None
            else self._resampler.resample_chunk(
                np.empty(0, dtype=np.int16),
                last=True,
            )
        )
        self._buffer.extend(converted.astype("<i2", copy=False).tobytes())
        return self._take_chunks(flush=True)


class RealtimeConnection:
    def __init__(
        self,
        *,
        session_service: AudioWorkflowSessionService,
        workflow_settings: WorkflowSettingsStore,
        max_message_bytes: int,
        diagnostics_enabled: bool,
        aha_version: str | None,
        models: RealtimeModelCatalog,
        inferencer_pool: InferencerPool | None,
        responses_limiter: ResizableConcurrencyLimiter | None,
        tts_limiter: ResizableConcurrencyLimiter | None,
        tool_registry: ToolRegistry,
        voice_resolver: VoiceResolver | None,
        received_audio_cache_dir: Path,
        agent_backend_gateway: AgentBackendGateway | None,
        model: str | None = None,
        target: RealtimeTarget | None = None,
    ) -> None:
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=workflow_settings.current().config.result_queue_capacity
        )
        self._closed = False
        self._output_closed = False
        self._input_finished = False
        self.max_message_bytes = max_message_bytes
        self._send_lock = asyncio.Lock()
        self._chunk_bytes = workflow_settings.current().config.min_chunk_bytes
        self._resampler = _OnlinePcmResampler(self._chunk_bytes)
        self._input_contract_locked = False
        self._session_service = session_service
        self._workflow_settings = workflow_settings
        self._diagnostics_enabled = diagnostics_enabled
        self._aha_version = aha_version
        self._models = models
        self._inferencer_pool = inferencer_pool
        self._responses_limiter = responses_limiter
        self._tts_limiter = tts_limiter
        self._tool_registry = tool_registry
        self._voice_resolver = voice_resolver
        self._received_audio_cache_dir = received_audio_cache_dir
        self._agent_backend_gateway = agent_backend_gateway
        self._dynamic_transcription = model is None and target is None
        self._session: RealtimeTranscriptionSession | None = None
        self._initial_model = model
        self._initial_target = target
        self._transcription_session_id = session_id()

    def _new_session(
        self,
        *,
        model: str,
        target: RealtimeTarget,
    ) -> RealtimeTranscriptionSession:
        return RealtimeTranscriptionSession(
            send_event=self._send,
            session_service=self._session_service,
            workflow_settings=self._workflow_settings,
            model=model,
            target=target,
            audio_event_handler=self._handle_audio_append,
            diagnostics_enabled=self._diagnostics_enabled,
            aha_version=self._aha_version,
            inferencer_pool=self._inferencer_pool,
            responses_limiter=self._responses_limiter,
            tts_limiter=self._tts_limiter,
            tool_registry=self._tool_registry,
            voice_resolver=self._voice_resolver,
            session_identifier=(
                self._transcription_session_id
                if not isinstance(target, ActiveVoiceAgent)
                else None
            ),
            received_audio_cache_dir=self._received_audio_cache_dir,
            agent_backend_gateway=self._agent_backend_gateway,
        )

    async def _send(self, event) -> None:
        payload = event_payload(event)
        validated = SERVER_EVENTS.validate_python(payload)
        if isinstance(validated, AhaV1ServerEventBase) and self._aha_version != "v1":
            raise RuntimeError("AHA extension event was not negotiated")
        async with self._send_lock:
            if not self._output_closed:
                await self._events.put(payload)

    async def start(self) -> None:
        if not self._dynamic_transcription:
            assert self._initial_model is not None
            assert self._initial_target is not None
            try:
                self._session = self._new_session(
                    model=self._initial_model,
                    target=self._initial_target,
                )
            except (RuntimeError, ValueError) as exc:
                error = await self.send_error(
                    message=str(exc),
                    code="model_workflow_invalid",
                    param="model",
                )
                raise RealtimeConnectionRejected(
                    "internal", "Model workflow invalid", event=error
                ) from exc
        if self._session is None:
            await self._send(
                AhaSessionCreatedEvent(
                    event_id=event_id(),
                    session=(
                        AhaRealtimeTranscriptionSessionCreateRequest.model_validate(
                            {
                                "type": "transcription",
                                "id": self._transcription_session_id,
                                "object": "realtime.transcription_session",
                                "audio": {
                                    "input": {
                                        "format": {
                                            "type": "audio/pcm",
                                            "rate": INPUT_SAMPLE_RATE,
                                        },
                                        "turn_detection": {"type": "server_vad"},
                                    }
                                },
                            }
                        )
                    ),
                    type="session.created",
                )
            )
        else:
            await self._session.start()

    async def handle_event(self, event: Any) -> None:
        if self._closed or self._input_finished:
            raise RuntimeError("Realtime 输入已经结束")
        try:
            event = CLIENT_EVENTS.validate_python(event)
        except ValidationError as exc:
            await self.send_error(
                message=f"Realtime 事件格式不合法: {exc.errors()[0]['msg']}",
                code="invalid_event",
                client_event_id=(
                    event.get("event_id")
                    if isinstance(event, dict)
                    else getattr(event, "event_id", None)
                ),
            )
            return
        if isinstance(event, InputAudioBufferClearEvent):
            self._reset_resampler()
        if (
            isinstance(event, SessionUpdateEvent)
            and self._input_contract_locked
            and self._update_changes_input_contract(event)
        ):
            await self.send_error(
                message=(
                    "收到音频后必须先 clear 或显式 commit，"
                    "才能修改音频格式或转写模型"
                ),
                code="session_update_after_audio",
                client_event_id=event.event_id,
                param="session",
            )
            return
        if self._dynamic_transcription and isinstance(event, SessionUpdateEvent):
            await self._handle_session_update(event)
        elif self._session is None:
            await self.send_error(
                message=("发送音频前必须先通过 session.update 配置转写模型"),
                code="session_not_configured",
                client_event_id=getattr(event, "event_id", None),
                param="type",
            )
        else:
            if isinstance(event, InputAudioBufferCommitEvent):
                for chunk in self._resampler.finish():
                    await self._session.append_engine_audio(chunk)
            await self._session.handle_client_event(event)
            if isinstance(event, InputAudioBufferCommitEvent):
                self._reset_resampler()
        if isinstance(event, SessionUpdateEvent):
            self._sync_resampler_rate()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            event = await self._events.get()
            try:
                yield event
            finally:
                self._events.task_done()

    async def drain_events(self) -> None:
        await self._events.join()

    async def finish_input(self) -> None:
        if self._input_finished or self._closed:
            return
        if self._session is not None:
            if self._input_contract_locked or self._session.audio_started:
                await self.handle_event(
                    InputAudioBufferCommitEvent(type="input_audio_buffer.commit")
                )
            await self._session.finish_input()
        self._input_finished = True

    def close_output(self) -> None:
        """Unblock producers before cancelling tasks after a transport failure."""
        self._output_closed = True
        while not self._events.empty():
            self._events.get_nowait()
            self._events.task_done()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_output()
        if self._session is not None:
            await self._session.close()

    def _update_changes_input_contract(self, event: SessionUpdateEvent) -> bool:
        if self._session is None:
            return False
        payload = event.session.model_dump(
            mode="python",
            exclude_none=False,
            exclude_unset=True,
        )
        audio_input = (payload.get("audio") or {}).get("input") or {}
        requested_format = audio_input.get("format")
        if requested_format is not None:
            requested_rate = requested_format.get("rate", INPUT_SAMPLE_RATE)
            current_rate = self._session.input_audio_format.get(
                "rate", INPUT_SAMPLE_RATE
            )
            if requested_rate != current_rate:
                return True
        if not self._dynamic_transcription:
            return False
        transcription = audio_input.get("transcription") or {}
        requested_model = transcription.get("model")
        return bool(
            isinstance(requested_model, str)
            and requested_model.strip()
            and requested_model.strip() != self._session.model
        )

    async def _handle_session_update(self, event: SessionUpdateEvent) -> None:
        current_model = self._session.model if self._session is not None else ""
        try:
            requested = AhaRealtimeTranscriptionSessionCreateRequest.model_validate(
                event.session.model_dump(
                    mode="python",
                    exclude_none=False,
                    exclude_unset=True,
                )
            )
            model, _ = session_values(
                requested,
                default_model=current_model,
                default_language=(
                    self._session.language
                    if self._session is not None
                    else (
                        self._workflow_settings.current().config.default_language_code
                    )
                ),
            )
            if not model:
                raise ValueError(
                    "首次 session.update 必须设置 " "audio.input.transcription.model"
                )
        except ValueError as exc:
            await self.send_error(
                message=str(exc),
                code="invalid_session",
                client_event_id=event.event_id,
                param="session",
            )
            return

        if self._session is not None and model == self._session.model:
            await self._session.handle_client_event(event)
            return
        if self._session is not None and self._session.audio_started:
            await self._session.handle_client_event(event)
            return

        try:
            target: RealtimeTarget = await self._models.resolve(model)
            if isinstance(target, ActiveVoiceAgent):
                raise ValueError("transcription intent 只能绑定语音转写工作流")
        except RealtimeModelNotFoundError as exc:
            await self.send_error(
                message=str(exc),
                code="model_not_found",
                client_event_id=event.event_id,
                param="session.audio.input.transcription.model",
            )
            return
        except RuntimeError as exc:
            await self.send_error(
                message=str(exc),
                code="model_workflow_not_active",
                client_event_id=event.event_id,
                param="session.audio.input.transcription.model",
            )
            return
        except ValueError as exc:
            await self.send_error(
                message=str(exc),
                code="invalid_session",
                client_event_id=event.event_id,
                param="session.audio.input.transcription.model",
            )
            return

        try:
            next_session = self._new_session(model=model, target=target)
        except (RuntimeError, ValueError) as exc:
            await self.send_error(
                message=str(exc),
                code="model_workflow_invalid",
                client_event_id=event.event_id,
                param="session.audio.input.transcription.model",
            )
            return
        previous = self._session
        self._session = next_session
        if previous is not None:
            await previous.close()
        await self._session.handle_client_event(event)

    async def send_error(
        self,
        *,
        message: str,
        code: str,
        client_event_id: str | None = None,
        param: str | None = None,
    ) -> dict[str, Any]:
        payload = event_payload(
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
        await self._send(payload)
        return payload

    async def _handle_audio_append(
        self,
        event: InputAudioBufferAppendEvent,
    ) -> None:
        assert self._session is not None
        audio = event.audio
        if len(audio) > self.max_message_bytes:
            await self._session.send_error(
                message="单个音频事件超过 max_message_bytes",
                code="audio_message_too_large",
                client_event_id=event.event_id,
                param="audio",
            )
            return
        try:
            chunks = self._resampler.append(audio)
        except ValueError as exc:
            await self._session.send_error(
                message=str(exc),
                code="invalid_audio",
                client_event_id=event.event_id,
                param="audio",
            )
            return
        if audio:
            self._input_contract_locked = True
        for chunk in chunks:
            await self._session.append_engine_audio(chunk)

    def _reset_resampler(self) -> None:
        self._resampler = _OnlinePcmResampler(
            self._chunk_bytes,
            self._input_sample_rate(),
        )
        self._input_contract_locked = False

    def _input_sample_rate(self) -> int:
        if self._session is None:
            return INPUT_SAMPLE_RATE
        return int(self._session.input_audio_format.get("rate", INPUT_SAMPLE_RATE))

    def _sync_resampler_rate(self) -> None:
        input_sample_rate = self._input_sample_rate()
        if self._resampler.input_sample_rate != input_sample_rate:
            self._resampler = _OnlinePcmResampler(
                self._chunk_bytes,
                input_sample_rate,
            )


class RealtimeService:
    def __init__(
        self,
        *,
        session_service: AudioWorkflowSessionService,
        workflow_settings: WorkflowSettingsStore,
        models: RealtimeModelCatalog,
        max_message_bytes: int,
        inferencer_pool: InferencerPool | None = None,
        responses_limiter: ResizableConcurrencyLimiter | None = None,
        tts_limiter: ResizableConcurrencyLimiter | None = None,
        tool_registry: ToolRegistry | None = None,
        voice_resolver: VoiceResolver | None = None,
        received_audio_cache_dir: Path = Path("./data-bin/cache"),
        agent_backend_gateway: AgentBackendGateway | None = None,
    ) -> None:
        self.session_service = session_service
        self.workflow_settings = workflow_settings
        self.models = models
        self.max_message_bytes = max_message_bytes
        self.inferencer_pool = inferencer_pool
        self.responses_limiter = responses_limiter
        self.tts_limiter = tts_limiter
        self.tool_registry = tool_registry or ToolRegistry()
        self.voice_resolver = voice_resolver
        self.received_audio_cache_dir = received_audio_cache_dir
        self.agent_backend_gateway = agent_backend_gateway

    async def open(self, options: RealtimeOptions) -> RealtimeConnection:
        if (options.intent is None) == (options.model is None):
            raise RealtimeConnectionRejected(
                "invalid_argument", "Specify exactly one of intent or model"
            )
        if options.intent not in (None, "transcription"):
            raise RealtimeConnectionRejected(
                "invalid_argument", "Unsupported realtime intent"
            )
        if options.aha_version not in (None, "v1"):
            raise RealtimeConnectionRejected(
                "invalid_argument", "Unsupported x_aha version"
            )
        target = None
        model = options.model
        if model is not None:
            model = model.strip()
            if not model:
                raise RealtimeConnectionRejected(
                    "invalid_argument", "model is required"
                )
            try:
                target = await self.models.resolve(model)
            except RealtimeModelNotFoundError as exc:
                raise RealtimeConnectionRejected(
                    "not_found", "Model not found"
                ) from exc
            except RuntimeError as exc:
                raise RealtimeConnectionRejected(
                    "failed_precondition", "Model workflow not active"
                ) from exc
            if not isinstance(target, ActiveVoiceAgent):
                raise RealtimeConnectionRejected(
                    "invalid_argument", "model must resolve to an active voice agent"
                )
        connection = RealtimeConnection(
            session_service=self.session_service,
            workflow_settings=self.workflow_settings,
            max_message_bytes=self.max_message_bytes,
            diagnostics_enabled=options.diagnostics_enabled,
            aha_version=options.aha_version,
            models=self.models,
            inferencer_pool=self.inferencer_pool,
            responses_limiter=self.responses_limiter,
            tts_limiter=self.tts_limiter,
            tool_registry=self.tool_registry,
            voice_resolver=self.voice_resolver,
            received_audio_cache_dir=self.received_audio_cache_dir,
            agent_backend_gateway=self.agent_backend_gateway,
            model=model,
            target=target,
        )
        try:
            await connection.start()
        except BaseException:
            await connection.close()
            raise
        return connection
