from __future__ import annotations

import asyncio
import base64
import binascii
import logging

import numpy as np
import soxr
from fastapi import WebSocket, WebSocketDisconnect
from openai.types.realtime import (
    InputAudioBufferAppendEvent,
    InputAudioBufferClearEvent,
    RealtimeClientEvent,
)
from pydantic import TypeAdapter, ValidationError

from audio_harness.capacity import ResizableConcurrencyLimiter
from audio_harness.domain import CapacityExceededError
from audio_harness.engines.pool import InferencerPool
from audio_harness.serving.realtime.session import (
    ENGINE_SAMPLE_RATE,
    INPUT_SAMPLE_RATE,
    RealtimeTranscriptionSession,
)
from audio_harness.serving.realtime.agent import VoiceResolver
from audio_harness.sessions import AudioWorkflowSessionService
from audio_harness.serving.realtime.tools import ToolRegistry
from audio_harness.workflow_settings import WorkflowSettingsStore
from audio_harness.voice_agents.service import (
    RealtimeRouteNotFoundError,
    RealtimeRouteService,
    RealtimeTarget,
)

logger = logging.getLogger(__name__)

_CLIENT_EVENT_ADAPTER = TypeAdapter(RealtimeClientEvent)


class _OnlinePcmResampler:
    def __init__(self, chunk_bytes: int) -> None:
        self.chunk_bytes = chunk_bytes
        self._resampler = soxr.ResampleStream(
            INPUT_SAMPLE_RATE,
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
        if len(audio) % 2:
            raise ValueError("PCM S16LE 音频字节数必须为偶数")
        if not audio:
            return []
        samples = np.frombuffer(audio, dtype="<i2")
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
        model: str,
        session_service: AudioWorkflowSessionService,
        workflow_settings: WorkflowSettingsStore,
        max_message_bytes: int,
        diagnostics_enabled: bool,
        target: RealtimeTarget,
        inferencer_pool: InferencerPool | None,
        responses_limiter: ResizableConcurrencyLimiter | None,
        tts_limiter: ResizableConcurrencyLimiter | None,
        tool_registry: ToolRegistry,
        voice_resolver: VoiceResolver | None,
    ) -> None:
        self.websocket = websocket
        self.max_message_bytes = max_message_bytes
        self._send_lock = asyncio.Lock()
        self._chunk_bytes = workflow_settings.current().config.min_chunk_bytes
        self._resampler = _OnlinePcmResampler(self._chunk_bytes)
        self._session = RealtimeTranscriptionSession(
            send_event=self._send,
            session_service=session_service,
            workflow_settings=workflow_settings,
            model=model,
            target=target,
            audio_event_handler=self._handle_audio_append,
            diagnostics_enabled=diagnostics_enabled,
            inferencer_pool=inferencer_pool,
            responses_limiter=responses_limiter,
            tts_limiter=tts_limiter,
            tool_registry=tool_registry,
            voice_resolver=voice_resolver,
        )

    async def _send(self, event) -> None:
        payload = (
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )
        async with self._send_lock:
            await self.websocket.send_json(payload)

    async def run(self) -> None:
        await self._session.start()
        try:
            while True:
                raw = await self.websocket.receive_json()
                try:
                    event = _CLIENT_EVENT_ADAPTER.validate_python(raw)
                except ValidationError as exc:
                    await self._session.send_error(
                        message=(
                            "Realtime 事件格式不合法: "
                            f"{exc.errors()[0]['msg']}"
                        ),
                        code="invalid_event",
                    )
                    continue
                if isinstance(event, InputAudioBufferClearEvent):
                    self._resampler = _OnlinePcmResampler(self._chunk_bytes)
                await self._session.handle_client_event(event)
        except WebSocketDisconnect:
            pass
        finally:
            await self._session.close()

    async def _handle_audio_append(
        self,
        event: InputAudioBufferAppendEvent,
    ) -> None:
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
        if len(audio) % 2:
            await self._session.send_error(
                message="PCM S16LE 音频字节数必须为偶数",
                code="invalid_pcm_alignment",
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
        for chunk in chunks:
            await self._session.append_engine_audio(chunk)


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
        model: str,
        diagnostics_enabled: bool = False,
    ) -> None:
        if not model.strip():
            await websocket.close(code=4400, reason="model is required")
            return
        try:
            target = self.routes.resolve(model.strip())
        except RealtimeRouteNotFoundError:
            await websocket.close(code=4404, reason="Model not found")
            return
        except RuntimeError:
            await websocket.close(code=4409, reason="Model workflow not active")
            return
        try:
            await websocket.accept()
            connection = _RealtimeConnection(
                websocket=websocket,
                model=model.strip(),
                session_service=self.session_service,
                workflow_settings=self.workflow_settings,
                max_message_bytes=self.max_message_bytes,
                diagnostics_enabled=diagnostics_enabled,
                target=target,
                inferencer_pool=self.inferencer_pool,
                responses_limiter=self.responses_limiter,
                tts_limiter=self.tts_limiter,
                tool_registry=self.tool_registry,
                voice_resolver=self.voice_resolver,
            )
            await connection.run()
        except CapacityExceededError:
            await websocket.close(
                code=4429,
                reason="Session capacity exceeded",
            )
