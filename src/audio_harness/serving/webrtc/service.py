from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from aiortc import (
    RTCConfiguration,
    RTCDataChannel,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from openai.types.realtime import (
    RealtimeClientEvent,
    RealtimeTranscriptionSessionCreateRequest,
)
from pydantic import TypeAdapter, ValidationError

from audio_harness.config import WebRtcConfig
from audio_harness.domain import CapacityExceededError
from audio_harness.pipelines.service import PipelineStore
from audio_harness.serving.realtime.session import (
    RealtimeTranscriptionSession,
    default_transcription_session,
)
from audio_harness.serving.webrtc.tracks import (
    IncomingAudioPump,
    ServerAudioTrack,
)
from audio_harness.sessions import (
    TranscriptionSessionReservation,
    TranscriptionSessionService,
)
from audio_harness.workflow_settings import WorkflowSettingsStore

logger = logging.getLogger(__name__)

_CLIENT_EVENT_ADAPTER = TypeAdapter(RealtimeClientEvent)


@dataclass(frozen=True)
class WebRtcCallAnswer:
    call_id: str
    sdp: str


class WebRtcCallNotFoundError(Exception):
    pass


class WebRtcCall:
    def __init__(
        self,
        *,
        call_id: str,
        configuration: RTCConfiguration,
        webrtc_config: WebRtcConfig,
        session_service: TranscriptionSessionService,
        workflow_settings: WorkflowSettingsStore,
        pipeline_store: PipelineStore,
        reservation: TranscriptionSessionReservation,
        reservation_context: Any,
        initial_session: RealtimeTranscriptionSessionCreateRequest,
        diagnostics_enabled: bool,
        on_closed: Callable[[str], Awaitable[None]],
    ) -> None:
        self.call_id = call_id
        self._config = webrtc_config
        self._pc = RTCPeerConnection(configuration)
        self._reservation_context = reservation_context
        self._on_closed = on_closed
        self._data_channel: RTCDataChannel | None = None
        self._data_ready = asyncio.Event()
        self._data_session_started = False
        self._audio_track_count = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._disconnect_task: asyncio.Task[None] | None = None
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._output_track = ServerAudioTrack()
        self._realtime = RealtimeTranscriptionSession(
            send_event=self._send_event,
            session_service=session_service,
            workflow_settings=workflow_settings,
            pipeline_store=pipeline_store,
            model="gpt-4o-transcribe",
            initial_session=initial_session,
            reservation=reservation,
            diagnostics_enabled=diagnostics_enabled,
        )
        self._chunk_bytes = workflow_settings.current().config.min_chunk_bytes
        self._install_handlers()

    @property
    def connection_state(self) -> str:
        return self._pc.connectionState

    @property
    def closed(self) -> bool:
        return self._closed

    async def negotiate(self, offer_sdp: str) -> str:
        await asyncio.wait_for(
            self._pc.setRemoteDescription(
                RTCSessionDescription(sdp=offer_sdp, type="offer")
            ),
            timeout=self._config.negotiation_timeout_seconds,
        )
        if self._audio_track_count == 0:
            raise ValueError("SDP offer 必须包含一条音频发送轨道")
        if self._audio_track_count > 1:
            raise ValueError("第一版只支持一条 WebRTC 音频输入轨道")
        if self._pc.sctp is None:
            raise ValueError("SDP offer 必须包含 oai-events DataChannel")
        self._pc.addTrack(self._output_track)
        answer = await asyncio.wait_for(
            self._pc.createAnswer(),
            timeout=self._config.negotiation_timeout_seconds,
        )
        await asyncio.wait_for(
            self._pc.setLocalDescription(answer),
            timeout=self._config.negotiation_timeout_seconds,
        )
        if self._pc.localDescription is None:
            raise RuntimeError("WebRTC 未生成本地 SDP answer")
        return self._pc.localDescription.sdp

    async def close(self, reason: str) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            logger.info("关闭 WebRTC call: call_id=%s reason=%s", self.call_id, reason)
            current = asyncio.current_task()
            for task in list(self._tasks):
                if task is not current and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in self._tasks
                    if task is not current
                ),
                return_exceptions=True,
            )
            if (
                self._disconnect_task is not None
                and self._disconnect_task is not current
            ):
                self._disconnect_task.cancel()
                await asyncio.gather(
                    self._disconnect_task,
                    return_exceptions=True,
                )
            await self._realtime.close()
            self._output_track.stop()
            await self._pc.close()
            await self._reservation_context.__aexit__(None, None, None)
            await self._on_closed(self.call_id)

    def _install_handlers(self) -> None:
        @self._pc.on("track")
        def on_track(track) -> None:
            if track.kind != "audio":
                track.stop()
                return
            self._audio_track_count += 1
            if self._audio_track_count > 1:
                track.stop()
                return
            self._spawn(self._pump_audio(track), "audio-pump")

        @self._pc.on("datachannel")
        def on_datachannel(channel: RTCDataChannel) -> None:
            if channel.label != "oai-events" or self._data_channel is not None:
                channel.close()
                self._spawn(
                    self.close("invalid_data_channel"),
                    "call-close",
                )
                return
            self._data_channel = channel

            @channel.on("open")
            def on_open() -> None:
                self._spawn(self._open_data_channel(), "data-channel-open")

            @channel.on("message")
            def on_message(message) -> None:
                self._spawn(
                    self._handle_data_message(message),
                    "data-channel-message",
                )

            @channel.on("close")
            def on_close() -> None:
                self._spawn(self.close("data_channel_closed"), "call-close")

            # aiortc 在远端创建的 DataChannel 上可能先切到 open，
            # 再向 PeerConnection 派发 datachannel 事件。
            if channel.readyState == "open":
                self._spawn(self._open_data_channel(), "data-channel-open")

        @self._pc.on("connectionstatechange")
        def on_connectionstatechange() -> None:
            state = self._pc.connectionState
            logger.info(
                "WebRTC connection state: call_id=%s state=%s",
                self.call_id,
                state,
            )
            if state in {"failed", "closed"}:
                self._spawn(self.close(f"connection_{state}"), "call-close")
            elif state == "disconnected":
                if (
                    self._disconnect_task is None
                    or self._disconnect_task.done()
                ):
                    self._disconnect_task = asyncio.create_task(
                        self._close_after_disconnect(),
                        name=f"webrtc-disconnect-{self.call_id}",
                    )
            elif state == "connected" and self._disconnect_task is not None:
                self._disconnect_task.cancel()
                self._disconnect_task = None

    async def _open_data_channel(self) -> None:
        if self._data_session_started:
            return
        self._data_session_started = True
        self._data_ready.set()
        await self._realtime.start()

    async def _handle_data_message(self, message: Any) -> None:
        if not isinstance(message, str):
            await self._realtime.send_error(
                message="oai-events 只接受 JSON 文本消息",
                code="invalid_event",
            )
            return
        try:
            raw = json.loads(message)
            event = _CLIENT_EVENT_ADAPTER.validate_python(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            await self._realtime.send_error(
                message=f"Realtime 事件格式不合法: {exc}",
                code="invalid_event",
            )
            return
        await self._realtime.handle_client_event(event)

    async def _pump_audio(self, track) -> None:
        await self._data_ready.wait()
        pump = IncomingAudioPump(
            track=track,
            chunk_bytes=self._chunk_bytes,
            append_audio=self._realtime.append_engine_audio,
        )
        try:
            await pump.run()
            await self._realtime.finish_audio()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("WebRTC 音频接收失败: call_id=%s", self.call_id)
            await self._realtime.send_error(
                message=str(exc),
                code="audio_track_failed",
            )

    async def _send_event(self, event: Any) -> None:
        channel = self._data_channel
        if channel is None or channel.readyState != "open":
            raise RuntimeError("oai-events DataChannel 尚未打开")
        payload = (
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )
        channel.send(
            json.dumps(
                payload,
                ensure_ascii=False,
            )
        )

    async def _close_after_disconnect(self) -> None:
        await asyncio.sleep(self._config.disconnected_grace_seconds)
        if self._pc.connectionState == "disconnected":
            await self.close("disconnect_timeout")

    def _spawn(self, coroutine, suffix: str) -> None:
        if self._closed:
            coroutine.close()
            return
        task = asyncio.create_task(
            coroutine,
            name=f"webrtc-{suffix}-{self.call_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class WebRtcCallService:
    def __init__(
        self,
        *,
        config: WebRtcConfig,
        session_service: TranscriptionSessionService,
        workflow_settings: WorkflowSettingsStore,
        pipeline_store: PipelineStore,
        api_key: str,
        max_message_bytes: int,
    ) -> None:
        self._config = config
        self._session_service = session_service
        self._workflow_settings = workflow_settings
        self._pipeline_store = pipeline_store
        self._api_key = api_key
        self._max_message_bytes = max_message_bytes
        self._calls: dict[str, WebRtcCall] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._rtc_configuration = RTCConfiguration(
            iceServers=[
                RTCIceServer(
                    urls=server.urls,
                    username=server.username,
                    credential=(
                        server.credential.get_secret_value()
                        if server.credential is not None
                        else None
                    ),
                )
                for server in config.ice_servers
            ]
        )

    @property
    def active_calls(self) -> int:
        return len(self._calls)

    def authorize(self, authorization: str) -> bool:
        return hmac.compare_digest(
            authorization,
            f"Bearer {self._api_key}",
        )

    async def create_call(
        self,
        *,
        offer_sdp: str,
        session: RealtimeTranscriptionSessionCreateRequest | None,
        diagnostics_enabled: bool = False,
    ) -> WebRtcCallAnswer:
        if self._closed:
            raise RuntimeError("WebRTC 服务正在关闭")
        if not offer_sdp.strip():
            raise ValueError("sdp 不能为空")
        if len(offer_sdp.encode("utf-8")) > self._max_message_bytes:
            raise ValueError("sdp 超过 max_message_bytes")
        workflow = self._workflow_settings.current().config
        physical_vad = self._pipeline_store.physical_vad_config()
        initial_session = session or default_transcription_session(
            model="gpt-4o-transcribe",
            language=workflow.default_language_code,
            prefix_padding_ms=round(
                physical_vad.speech_pre_roll_seconds * 1000
            ),
        )
        call_id = f"call_{uuid4().hex}"
        reservation_context = self._session_service.reserve()
        try:
            reservation = await reservation_context.__aenter__()
        except CapacityExceededError:
            raise
        call = WebRtcCall(
            call_id=call_id,
            configuration=self._rtc_configuration,
            webrtc_config=self._config,
            session_service=self._session_service,
            workflow_settings=self._workflow_settings,
            pipeline_store=self._pipeline_store,
            reservation=reservation,
            reservation_context=reservation_context,
            initial_session=initial_session,
            diagnostics_enabled=diagnostics_enabled,
            on_closed=self._discard,
        )
        try:
            answer_sdp = await call.negotiate(offer_sdp)
            async with self._lock:
                if self._closed or call.closed:
                    raise RuntimeError("WebRTC 服务正在关闭")
                self._calls[call_id] = call
            logger.info("创建 WebRTC call: call_id=%s", call_id)
            return WebRtcCallAnswer(call_id=call_id, sdp=answer_sdp)
        except Exception:
            await call.close("negotiation_failed")
            raise

    async def hangup(self, call_id: str) -> None:
        async with self._lock:
            call = self._calls.get(call_id)
        if call is None:
            raise WebRtcCallNotFoundError(call_id)
        await call.close("hangup")

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            calls = list(self._calls.values())
        await asyncio.gather(
            *(call.close("service_shutdown") for call in calls),
            return_exceptions=True,
        )

    async def _discard(self, call_id: str) -> None:
        async with self._lock:
            self._calls.pop(call_id, None)
