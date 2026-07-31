from __future__ import annotations

import asyncio
import fractions
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import numpy as np
import pytest
from aiortc import (
    AudioStreamTrack,
    MediaStreamError,
    RTCPeerConnection,
    RTCSessionDescription,
)
from av import AudioFrame
from openai.types.realtime import RealtimeServerEvent
from pydantic import TypeAdapter

from audio_harness.capacity import ServingCapacity
from audio_harness.config import CapacityConfig, WebRtcConfig, WorkflowConfig
from audio_harness.domain import (
    AudioSegment,
    EngineSet,
    RecognitionOptions,
    TranscriptionResult,
    VadBoundary,
    VadUpdate,
)
from audio_harness.serving.webrtc import WebRtcCallService
from audio_harness.sessions import TranscriptionSessionService
from audio_harness.workflow_settings import WorkflowSettingsSnapshot

_SERVER_EVENT_ADAPTER = TypeAdapter(RealtimeServerEvent)


class _ImmediateVad:
    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadUpdate]:
        async for chunk in audio_chunks:
            samples = len(chunk) // 2
            yield VadUpdate(
                boundaries=(VadBoundary(0, samples),),
                processed_samples=samples,
            )
            return

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        return True


class _FinalAsr:
    async def transcribe(
        self,
        segment: AudioSegment,
        options: RecognitionOptions,
    ) -> AsyncIterator[TranscriptionResult]:
        yield TranscriptionResult(
            transcript="WebRTC 转写成功",
            is_final=True,
            start_sample=segment.start_sample,
            end_sample=segment.end_sample,
            sample_rate=options.sample_rate,
            turn_completed=True,
            language_code=options.language_code,
        )


class _StaticProvider:
    @asynccontextmanager
    async def lease(self):
        yield EngineSet(
            asr=_FinalAsr(),
            vad=_ImmediateVad(),
            semantic_vad=None,
        )


class _StaticSettings:
    def __init__(self) -> None:
        self._snapshot = WorkflowSettingsSnapshot(
            config=WorkflowConfig(
                min_chunk_bytes=6400,
                min_segment_seconds=0,
            ),
            revision=1,
            updated_at=datetime.now(timezone.utc),
        )

    def current(self) -> WorkflowSettingsSnapshot:
        return self._snapshot


class _FiniteAudioTrack(AudioStreamTrack):
    def __init__(self, frames: int = 20) -> None:
        super().__init__()
        self._remaining = frames
        self._pts = 0

    async def recv(self) -> AudioFrame:
        if self._remaining <= 0:
            self.stop()
            raise MediaStreamError
        frame = AudioFrame(format="s16", layout="mono", samples=960)
        frame.planes[0].update(np.zeros(960, dtype="<i2").tobytes())
        frame.sample_rate = 48_000
        frame.time_base = fractions.Fraction(1, 48_000)
        frame.pts = self._pts
        self._pts += 960
        self._remaining -= 1
        await asyncio.sleep(0.02)
        return frame


def _build_service() -> tuple[WebRtcCallService, ServingCapacity]:
    settings = _StaticSettings()
    capacity = ServingCapacity(CapacityConfig())
    sessions = TranscriptionSessionService(
        engine_provider=_StaticProvider(),
        workflow_settings=settings,
        session_limiter=capacity.webrtc_sessions,
        asr_limiter=capacity.asr,
    )
    return (
        WebRtcCallService(
            config=WebRtcConfig(),
            session_service=sessions,
            workflow_settings=settings,
            api_key="local-test-key",
            max_message_bytes=1024 * 1024,
        ),
        capacity,
    )


@pytest.mark.asyncio
async def test_webrtc_call_transcribes_rtp_audio_and_releases_capacity() -> None:
    service, capacity = _build_service()
    client = RTCPeerConnection()
    events: asyncio.Queue[RealtimeServerEvent] = asyncio.Queue()
    data_channel = client.createDataChannel("oai-events")
    client.addTrack(_FiniteAudioTrack())

    @data_channel.on("message")
    def on_message(message) -> None:
        if isinstance(message, str):
            events.put_nowait(
                _SERVER_EVENT_ADAPTER.validate_python(json.loads(message))
            )

    offer = await client.createOffer()
    await client.setLocalDescription(offer)
    assert client.localDescription is not None

    answer = await service.create_call(
        offer_sdp=client.localDescription.sdp,
        session=None,
    )
    try:
        assert answer.call_id.startswith("call_")
        assert service.active_calls == 1
        assert capacity.webrtc_sessions.active == 1
        await client.setRemoteDescription(
            RTCSessionDescription(sdp=answer.sdp, type="answer")
        )

        received: list[RealtimeServerEvent] = []
        while not any(
            event.type
            == "conversation.item.input_audio_transcription.completed"
            for event in received
        ):
            received.append(
                await asyncio.wait_for(events.get(), timeout=15)
            )

        assert received[0].type == "session.created"
        assert received[-1].transcript == "WebRTC 转写成功"

        await service.hangup(answer.call_id)
        assert service.active_calls == 0
        assert capacity.webrtc_sessions.active == 0
    finally:
        await client.close()
        await service.close()


def test_webrtc_bearer_authentication() -> None:
    service, _capacity = _build_service()
    assert service.authorize("Bearer local-test-key")
    assert not service.authorize("Bearer wrong")
