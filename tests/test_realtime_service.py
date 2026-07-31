from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import numpy as np
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from openai.types.realtime import RealtimeServerEvent
from pydantic import TypeAdapter
from starlette.websockets import WebSocketDisconnect

from audio_harness.capacity import ServingCapacity
from audio_harness.config import CapacityConfig, WorkflowConfig
from audio_harness.domain import (
    AudioSegment,
    EngineSet,
    RecognitionOptions,
    TranscriptionResult,
    VadBoundary,
    VadUpdate,
)
from audio_harness.serving.realtime.connection import (
    RealtimeTranscriptionService,
    _OnlinePcmResampler,
)
from audio_harness.sessions import TranscriptionSessionService
from audio_harness.workflow_settings import WorkflowSettingsSnapshot

_SERVER_EVENT_ADAPTER = TypeAdapter(RealtimeServerEvent)


class ImmediateVad:
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


class ProgressVad:
    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadUpdate]:
        async for chunk in audio_chunks:
            samples = len(chunk) // 2
            yield VadUpdate(boundaries=(), processed_samples=samples // 2)
            yield VadUpdate(boundaries=(), processed_samples=samples)
            return

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        return True


class FinalAsr:
    async def transcribe(
        self,
        segment: AudioSegment,
        options: RecognitionOptions,
    ) -> AsyncIterator[TranscriptionResult]:
        yield TranscriptionResult(
            transcript="你好，世界",
            is_final=True,
            start_sample=segment.start_sample,
            end_sample=segment.end_sample,
            sample_rate=options.sample_rate,
            turn_completed=True,
            language_code=options.language_code,
        )


class StaticProvider:
    def __init__(self, vad: ImmediateVad | ProgressVad | None = None) -> None:
        self._vad = vad or ImmediateVad()

    @asynccontextmanager
    async def lease(self):
        yield EngineSet(
            asr=FinalAsr(),
            vad=self._vad,
            semantic_vad=None,
        )


class StaticSettings:
    def __init__(self, config: WorkflowConfig) -> None:
        self._snapshot = WorkflowSettingsSnapshot(
            config=config,
            revision=1,
            updated_at=datetime.now(timezone.utc),
        )

    def current(self) -> WorkflowSettingsSnapshot:
        return self._snapshot


def build_app(vad: ImmediateVad | ProgressVad | None = None) -> FastAPI:
    config = WorkflowConfig(
        min_chunk_bytes=6400,
        min_segment_seconds=0,
    )
    settings = StaticSettings(config)
    capacity = ServingCapacity(CapacityConfig())
    sessions = TranscriptionSessionService(
        engine_provider=StaticProvider(vad),
        workflow_settings=settings,
        session_limiter=capacity.realtime_sessions,
        asr_limiter=capacity.asr,
    )
    service = RealtimeTranscriptionService(
        session_service=sessions,
        workflow_settings=settings,
        api_key="local-test-key",
        max_message_bytes=1024 * 1024,
    )
    app = FastAPI()

    @app.websocket("/v1/realtime")
    async def realtime(websocket: WebSocket, model: str) -> None:
        await service.handle(websocket, model=model)

    return app


def receive_official_event(websocket):
    while True:
        payload = websocket.receive_json()
        if payload.get("type") != "audio_harness.vad_progress":
            return _SERVER_EVENT_ADAPTER.validate_python(payload)


def test_realtime_rejects_invalid_bearer_token() -> None:
    with TestClient(build_app()) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(
                "/v1/realtime?model=gpt-4o-transcribe",
                headers={"Authorization": "Bearer wrong"},
            ):
                pass
    assert error.value.code == 4401


def test_realtime_server_vad_event_sequence_uses_sdk_models() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?model=gpt-4o-transcribe",
            headers={"Authorization": "Bearer local-test-key"},
        ) as websocket:
            created = receive_official_event(websocket)
            assert created.type == "session.created"

            websocket.send_json(
                {
                    "type": "session.update",
                    "event_id": "client-session",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "format": {
                                    "type": "audio/pcm",
                                    "rate": 24000,
                                },
                                "transcription": {
                                    "model": "gpt-4o-transcribe",
                                    "language": "zh-CN",
                                },
                                "turn_detection": {"type": "server_vad"},
                            }
                        },
                    },
                }
            )
            assert receive_official_event(websocket).type == "session.updated"

            audio = np.zeros(24_000, dtype="<i2").tobytes()
            websocket.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "event_id": "client-audio",
                    "audio": base64.b64encode(audio).decode("ascii"),
                }
            )
            events = [receive_official_event(websocket) for _ in range(6)]
            assert [event.type for event in events] == [
                "input_audio_buffer.speech_started",
                "input_audio_buffer.speech_stopped",
                "input_audio_buffer.committed",
                "conversation.item.created",
                "conversation.item.input_audio_transcription.delta",
                "conversation.item.input_audio_transcription.completed",
            ]
            item_ids = {
                event.item_id
                for event in events
                if hasattr(event, "item_id")
            }
            assert len(item_ids) == 1
            assert events[-1].transcript == "你好，世界"


def test_realtime_emits_vad_progress_for_every_vad_update() -> None:
    with TestClient(build_app(ProgressVad())) as client:
        with client.websocket_connect(
            "/v1/realtime?model=gpt-4o-transcribe",
            headers={"Authorization": "Bearer local-test-key"},
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            websocket.send_json(
                {
                    "type": "session.update",
                    "session": {"type": "transcription"},
                }
            )
            assert receive_official_event(websocket).type == "session.updated"

            audio = np.zeros(24_000, dtype="<i2").tobytes()
            websocket.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio).decode("ascii"),
                }
            )
            progress = [websocket.receive_json() for _ in range(2)]

            assert [event["type"] for event in progress] == [
                "audio_harness.vad_progress",
                "audio_harness.vad_progress",
            ]
            assert [event["processed_audio_ms"] for event in progress] == [
                pytest.approx(3_200 / 2 / 16_000 * 1_000),
                pytest.approx(3_200 / 16_000 * 1_000),
            ]


def test_realtime_rejects_manual_commit_without_closing_connection() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?model=gpt-4o-transcribe",
            headers={"Authorization": "Bearer local-test-key"},
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            websocket.send_json(
                {
                    "type": "input_audio_buffer.commit",
                    "event_id": "manual-commit",
                }
            )
            error = receive_official_event(websocket)
            assert error.type == "error"
            assert error.error.code == "unsupported_manual_commit"

            websocket.send_json({"type": "input_audio_buffer.clear"})
            assert (
                receive_official_event(websocket).type
                == "input_audio_buffer.cleared"
            )


def test_soxr_online_resampler_preserves_one_second_duration() -> None:
    resampler = _OnlinePcmResampler(chunk_bytes=6400)
    chunks: list[bytes] = []
    input_audio = np.arange(24_000, dtype=np.int16).tobytes()
    for offset in range(0, len(input_audio), 4800):
        chunks.extend(resampler.append(input_audio[offset : offset + 4800]))
    chunks.extend(resampler.finish())

    assert sum(map(len, chunks)) // 2 == 16_000
    assert all(len(chunk) <= 6400 for chunk in chunks)
