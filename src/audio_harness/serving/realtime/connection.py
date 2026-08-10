from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from enum import Enum

import numpy as np
import soxr
from fastapi import WebSocket, WebSocketDisconnect
from openai.types.realtime import (
    InputAudioBufferAppendEvent,
    InputAudioBufferClearEvent,
    InputAudioBufferCommitEvent,
    RealtimeClientEvent,
    RealtimeError,
    RealtimeErrorEvent,
    RealtimeServerEvent,
    RealtimeTranscriptionSessionCreateRequest,
    SessionCreatedEvent,
    SessionUpdateEvent,
)
from pydantic import TypeAdapter, ValidationError

from audio_harness.capacity import ResizableConcurrencyLimiter
from audio_harness.domain import CapacityExceededError
from audio_harness.engines.pool import InferencerPool
from audio_harness.serving.realtime.session import (
    ENGINE_SAMPLE_RATE,
    INPUT_SAMPLE_RATE,
    RealtimeTranscriptionSession,
    event_id,
    session_id,
    session_values,
)
from audio_harness.serving.realtime.conversation import decode_wire_audio
from audio_harness.serving.realtime.agent import VoiceResolver
from audio_harness.sessions import AudioWorkflowSessionService
from audio_harness.serving.realtime.tools import ToolRegistry
from audio_harness.workflow_settings import WorkflowSettingsStore
from audio_harness.voice_agents.service import (
    ActiveVoiceAgent,
    RealtimeRouteNotFoundError,
    RealtimeRouteService,
    RealtimeTarget,
)

logger = logging.getLogger(__name__)

_CLIENT_EVENT_ADAPTER = TypeAdapter(RealtimeClientEvent)
_SERVER_EVENT_ADAPTER = TypeAdapter(RealtimeServerEvent)


class RealtimeConnectionMode(str, Enum):
    """Wire-level Realtime modes supported by the data-plane endpoint."""

    TRANSCRIPTION = "transcription"
    REALTIME = "realtime"


class _OnlinePcmResampler:
    def __init__(
        self,
        chunk_bytes: int,
        *,
        format_type: str = "audio/pcm",
        input_sample_rate: int = INPUT_SAMPLE_RATE,
    ) -> None:
        self.chunk_bytes = chunk_bytes
        self.format_type = format_type
        self.input_sample_rate = input_sample_rate
        self._resampler = soxr.ResampleStream(
            input_sample_rate,
            ENGINE_SAMPLE_RATE,
            1,
            dtype="int16",
            quality="HQ",
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
        pcm = decode_wire_audio(audio, format_type=self.format_type)
        samples = np.frombuffer(pcm, dtype="<i2")
        converted = self._resampler.resample_chunk(samples, last=False)
        self._buffer.extend(converted.astype("<i2", copy=False).tobytes())
        return self._take_chunks(flush=False)

    def finish(self) -> list[bytes]:
        if self._finished:
            return []
        self._finished = True
        converted = self._resampler.resample_chunk(
            np.empty(0, dtype=np.int16),
            last=True,
        )
        self._buffer.extend(converted.astype("<i2", copy=False).tobytes())
        return self._take_chunks(flush=True)


class _RealtimeConnection:
    def __init__(
        self,
        *,
        websocket: WebSocket,
        session_service: AudioWorkflowSessionService,
        workflow_settings: WorkflowSettingsStore,
        max_message_bytes: int,
        diagnostics_enabled: bool,
        aha_version: str | None,
        routes: RealtimeRouteService,
        inferencer_pool: InferencerPool | None,
        responses_limiter: ResizableConcurrencyLimiter | None,
        tts_limiter: ResizableConcurrencyLimiter | None,
        tool_registry: ToolRegistry,
        voice_resolver: VoiceResolver | None,
        model: str | None = None,
        target: RealtimeTarget | None = None,
    ) -> None:
        self.websocket = websocket
        self.max_message_bytes = max_message_bytes
        self._send_lock = asyncio.Lock()
        self._chunk_bytes = workflow_settings.current().config.min_chunk_bytes
        self._resampler = _OnlinePcmResampler(self._chunk_bytes)
        self._resampler_key = ("audio/pcm", INPUT_SAMPLE_RATE)
        self._input_contract_locked = False
        self._session_service = session_service
        self._workflow_settings = workflow_settings
        self._diagnostics_enabled = diagnostics_enabled
        self._aha_version = aha_version
        self._routes = routes
        self._inferencer_pool = inferencer_pool
        self._responses_limiter = responses_limiter
        self._tts_limiter = tts_limiter
        self._tool_registry = tool_registry
        self._voice_resolver = voice_resolver
        self._dynamic_transcription = model is None and target is None
        self._session: RealtimeTranscriptionSession | None = None
        self._transcription_session_id = session_id()
        if model is not None and target is not None:
            self._session = self._new_session(model=model, target=target)

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
        )

    async def _send(self, event) -> None:
        payload = (
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )
        event_type = str(payload.get("type", ""))
        if event_type.startswith("x_aha."):
            if self._aha_version != "v1":
                raise RuntimeError("AHA extension event was not negotiated")
        else:
            _SERVER_EVENT_ADAPTER.validate_python(payload)
        async with self._send_lock:
            await self.websocket.send_json(payload)

    async def run(self) -> None:
        if self._session is None:
            await self._send(
                SessionCreatedEvent(
                    event_id=event_id(),
                    session=RealtimeTranscriptionSessionCreateRequest.model_validate(
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
                    ),
                    type="session.created",
                )
            )
        else:
            await self._session.start()
        try:
            while True:
                raw = await self._receive_payload()
                if raw is None:
                    continue
                try:
                    event = _CLIENT_EVENT_ADAPTER.validate_python(raw)
                except ValidationError as exc:
                    await self._send_error(
                        message=(
                            "Realtime 事件格式不合法: "
                            f"{exc.errors()[0]['msg']}"
                        ),
                        code="invalid_event",
                    )
                    continue
                if isinstance(event, InputAudioBufferClearEvent):
                    self._reset_resampler()
                if (
                    isinstance(event, SessionUpdateEvent)
                    and self._input_contract_locked
                    and self._update_changes_input_contract(event)
                ):
                    await self._send_error(
                        message=(
                            "收到音频后必须先 clear 或显式 commit，"
                            "才能修改音频格式或转写模型"
                        ),
                        code="session_update_after_audio",
                        client_event_id=event.event_id,
                        param="session",
                    )
                    continue
                if (
                    self._dynamic_transcription
                    and isinstance(event, SessionUpdateEvent)
                ):
                    await self._handle_session_update(event)
                elif self._session is None:
                    await self._send_error(
                        message=(
                            "发送音频前必须先通过 session.update 配置转写模型"
                        ),
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
        except WebSocketDisconnect:
            pass
        finally:
            if self._session is not None:
                await self._session.close()

    async def _receive_payload(self) -> dict[str, object] | None:
        message = await self.websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(
                code=message.get("code", 1000),
                reason=message.get("reason"),
            )
        raw_message = message.get("text")
        if raw_message is None:
            raw_message = message.get("bytes")
        if isinstance(raw_message, bytes):
            if len(raw_message) > self.max_message_bytes:
                await self._send_error(
                    message="Realtime 事件超过 max_message_bytes",
                    code="message_too_large",
                )
                return None
            try:
                raw_message = raw_message.decode("utf-8")
            except UnicodeDecodeError:
                await self._send_error(
                    message="Realtime 事件必须是 UTF-8 JSON 文本",
                    code="invalid_event",
                )
                return None
        if not isinstance(raw_message, str):
            await self._send_error(
                message="Realtime 事件必须是 JSON 文本",
                code="invalid_event",
            )
            return None
        if len(raw_message.encode("utf-8")) > self.max_message_bytes:
            await self._send_error(
                message="Realtime 事件超过 max_message_bytes",
                code="message_too_large",
            )
            return None
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            await self._send_error(
                message="Realtime 事件不是合法 JSON",
                code="invalid_event",
            )
            return None
        if not isinstance(payload, dict):
            await self._send_error(
                message="Realtime 事件必须是 JSON 对象",
                code="invalid_event",
            )
            return None
        return payload

    def _update_changes_input_contract(self, event: SessionUpdateEvent) -> bool:
        if self._session is None:
            return False
        payload = event.session.model_dump(
            mode="json",
            exclude_none=False,
            exclude_unset=True,
        )
        audio_input = ((payload.get("audio") or {}).get("input") or {})
        requested_format = audio_input.get("format")
        if requested_format is not None:
            current_format = self._session.input_audio_format
            requested_type = requested_format.get("type")
            current_type = current_format.get("type")
            requested_rate = (
                requested_format.get("rate", INPUT_SAMPLE_RATE)
                if requested_type == "audio/pcm"
                else None
            )
            current_rate = (
                current_format.get("rate", INPUT_SAMPLE_RATE)
                if current_type == "audio/pcm"
                else None
            )
            if (requested_type, requested_rate) != (
                current_type,
                current_rate,
            ):
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
            requested = RealtimeTranscriptionSessionCreateRequest.model_validate(
                event.session.model_dump(
                    mode="json",
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
                    else self._workflow_settings.current().config.default_language_code
                ),
            )
            if not model:
                raise ValueError(
                    "首次 session.update 必须设置 "
                    "audio.input.transcription.model"
                )
        except ValueError as exc:
            await self._send_error(
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
            target: RealtimeTarget = self._routes.resolve(model)
            if isinstance(target, ActiveVoiceAgent):
                raise ValueError("transcription intent 只能绑定语音转写工作流")
        except RealtimeRouteNotFoundError as exc:
            await self._send_error(
                message=str(exc),
                code="model_not_found",
                client_event_id=event.event_id,
                param="session.audio.input.transcription.model",
            )
            return
        except RuntimeError as exc:
            await self._send_error(
                message=str(exc),
                code="model_workflow_not_active",
                client_event_id=event.event_id,
                param="session.audio.input.transcription.model",
            )
            return
        except ValueError as exc:
            await self._send_error(
                message=str(exc),
                code="invalid_session",
                client_event_id=event.event_id,
                param="session.audio.input.transcription.model",
            )
            return

        previous = self._session
        self._session = self._new_session(model=model, target=target)
        if previous is not None:
            await previous.close()
        await self._session.handle_client_event(event)

    async def _send_error(
        self,
        *,
        message: str,
        code: str,
        client_event_id: str | None = None,
        param: str | None = None,
    ) -> None:
        await self._send(
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

    async def _handle_audio_append(
        self,
        event: InputAudioBufferAppendEvent,
    ) -> None:
        assert self._session is not None
        self._ensure_audio_format()
        try:
            audio = base64.b64decode(event.audio, validate=True)
        except (binascii.Error, ValueError):
            await self._session.send_error(
                message="audio 必须是合法的 base64 PCM",
                code="invalid_audio_base64",
                client_event_id=event.event_id,
                param="audio",
            )
            return
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

    def _ensure_audio_format(self) -> None:
        assert self._session is not None
        audio_format = self._session.input_audio_format
        format_type = str(audio_format["type"])
        sample_rate = (
            int(audio_format.get("rate", INPUT_SAMPLE_RATE))
            if format_type == "audio/pcm"
            else 8_000
        )
        key = (format_type, sample_rate)
        if key == self._resampler_key:
            return
        self._resampler_key = key
        self._resampler = _OnlinePcmResampler(
            self._chunk_bytes,
            format_type=format_type,
            input_sample_rate=sample_rate,
        )

    def _reset_resampler(self) -> None:
        format_type, sample_rate = self._resampler_key
        self._resampler = _OnlinePcmResampler(
            self._chunk_bytes,
            format_type=format_type,
            input_sample_rate=sample_rate,
        )
        self._input_contract_locked = False


class RealtimeTranscriptionService:
    def __init__(
        self,
        *,
        session_service: AudioWorkflowSessionService,
        workflow_settings: WorkflowSettingsStore,
        routes: RealtimeRouteService,
        max_message_bytes: int,
        inferencer_pool: InferencerPool | None = None,
        responses_limiter: ResizableConcurrencyLimiter | None = None,
        tts_limiter: ResizableConcurrencyLimiter | None = None,
        tool_registry: ToolRegistry | None = None,
        voice_resolver: VoiceResolver | None = None,
    ) -> None:
        self.session_service = session_service
        self.workflow_settings = workflow_settings
        self.routes = routes
        self.max_message_bytes = max_message_bytes
        self.inferencer_pool = inferencer_pool
        self.responses_limiter = responses_limiter
        self.tts_limiter = tts_limiter
        self.tool_registry = tool_registry or ToolRegistry()
        self.voice_resolver = voice_resolver

    async def handle(
        self,
        websocket: WebSocket,
        *,
        mode: RealtimeConnectionMode = RealtimeConnectionMode.TRANSCRIPTION,
        model: str | None = None,
        diagnostics_enabled: bool = False,
        aha_version: str | None = None,
    ) -> None:
        target: RealtimeTarget | None = None
        selected_model: str | None = None
        if mode is RealtimeConnectionMode.REALTIME:
            selected_model = (model or "").strip()
            if not selected_model:
                await websocket.close(code=4400, reason="model is required")
                return
            try:
                target = self.routes.resolve(selected_model)
            except RealtimeRouteNotFoundError:
                await websocket.close(code=4404, reason="Model not found")
                return
            except RuntimeError:
                await websocket.close(
                    code=4409,
                    reason="Model workflow not active",
                )
                return
            if not isinstance(target, ActiveVoiceAgent):
                await websocket.close(
                    code=4400,
                    reason="model must resolve to an active voice agent",
                )
                return
        try:
            await websocket.accept()
            connection = _RealtimeConnection(
                websocket=websocket,
                session_service=self.session_service,
                workflow_settings=self.workflow_settings,
                max_message_bytes=self.max_message_bytes,
                diagnostics_enabled=diagnostics_enabled,
                aha_version=aha_version,
                routes=self.routes,
                inferencer_pool=self.inferencer_pool,
                responses_limiter=self.responses_limiter,
                tts_limiter=self.tts_limiter,
                tool_registry=self.tool_registry,
                voice_resolver=self.voice_resolver,
                model=selected_model,
                target=target,
            )
            await connection.run()
        except CapacityExceededError:
            await websocket.close(
                code=4429,
                reason="Session capacity exceeded",
            )
