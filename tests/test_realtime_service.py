from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import numpy as np
import pytest
from fastapi import FastAPI, Query, WebSocket
from fastapi.testclient import TestClient
from openai.types.realtime import RealtimeServerEvent
from pydantic import TypeAdapter
from starlette.websockets import WebSocketDisconnect

from audio_harness.capacity import ServingCapacity
from audio_harness.config import CapacityConfig, WorkflowRuntimeConfig
from audio_harness.domain import (
    AudioSegment,
    RecognitionOptions,
    TranscriptionResult,
    VadBoundary,
    VadUpdate,
)
from audio_harness.serving.realtime.connection import (
    RealtimeTranscriptionService,
    _OnlinePcmResampler,
)
from audio_harness.serving.http.routers.data_plane import create_data_plane_router
from audio_harness.sessions import AudioWorkflowSessionService
from audio_harness.workflow_settings import WorkflowSettingsSnapshot
from tests.graph_fakes import StaticRouteService, static_audio_workflow

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


class TwoSegmentVad(ImmediateVad):
    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadUpdate]:
        async for chunk in audio_chunks:
            samples = len(chunk) // 2
            middle = samples // 2
            yield VadUpdate(
                boundaries=(
                    VadBoundary(0, middle),
                    VadBoundary(middle, samples),
                ),
                processed_samples=samples,
            )
            return


class PendingThenCompleteVad(ImmediateVad):
    def __init__(self) -> None:
        self.calls = 0

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        self.calls += 1
        return self.calls > 1


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


class StaticSettings:
    def __init__(self, config: WorkflowRuntimeConfig) -> None:
        self._snapshot = WorkflowSettingsSnapshot(
            config=config,
            grpc_audio_workflow_id="default-audio",
            revision=1,
            updated_at=datetime.now(timezone.utc),
        )

    def current(self) -> WorkflowSettingsSnapshot:
        return self._snapshot


def build_app(
    vad: ImmediateVad | None = None,
    semantic_vad: ImmediateVad | None = None,
) -> FastAPI:
    config = WorkflowRuntimeConfig(
        min_chunk_bytes=6400,
    )
    settings = StaticSettings(config)
    capacity = ServingCapacity(CapacityConfig())
    selected_vad = vad or ImmediateVad()
    inferencer_pool, workflows, workflow = static_audio_workflow(
        asr=FinalAsr(),
        vad=selected_vad,
        semantic_vad=semantic_vad or selected_vad,
    )
    sessions = AudioWorkflowSessionService(
        inferencer_pool=inferencer_pool,
        workflows=workflows,
        workflow_settings=settings,
        session_limiter=capacity.realtime_sessions,
        asr_limiter=capacity.asr,
    )
    service = RealtimeTranscriptionService(
        session_service=sessions,
        workflow_settings=settings,
        routes=StaticRouteService(workflow),
        max_message_bytes=1024 * 1024,
        inferencer_pool=inferencer_pool,
    )
    app = FastAPI()

    @app.websocket("/v1/realtime")
    async def realtime(
        websocket: WebSocket,
        intent: str,
        diagnostics: bool = Query(default=False),
        x_aha: str | None = Query(default=None),
    ) -> None:
        if intent != "transcription" or "model" in websocket.query_params:
            await websocket.close(code=4400)
            return
        await service.handle(
            websocket,
            diagnostics_enabled=diagnostics,
            aha_version=x_aha,
        )

    return app


def receive_official_event(websocket):
    return _SERVER_EVENT_ADAPTER.validate_python(websocket.receive_json())


def configure_transcription(websocket, *, event_id: str = "client-session"):
    websocket.send_json(
        {
            "type": "session.update",
            "event_id": event_id,
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
    updated = receive_official_event(websocket)
    assert updated.type == "session.updated"
    return updated


class _UnexpectedDataPlaneService:
    async def handle(self, *args, **kwargs) -> None:
        raise AssertionError("不支持的 x_aha 版本不应进入 Realtime 服务")

    async def create_call(self, *args, **kwargs):
        raise AssertionError("不支持的 x_aha 版本不应进入 WebRTC 服务")

    async def hangup(self, call_id: str) -> None:
        raise AssertionError("不应调用 hangup")


def test_realtime_does_not_require_bearer_token() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            created = receive_official_event(websocket)
            assert created.type == "session.created"
            assert created.session.type == "transcription"


def test_realtime_requires_model_in_first_session_update() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            websocket.send_json({"audio": ""})
            invalid_event = receive_official_event(websocket)
            assert invalid_event.type == "error"
            assert invalid_event.error.code == "invalid_event"

            websocket.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "event_id": "early-audio",
                    "audio": "",
                }
            )
            early_error = receive_official_event(websocket)
            assert early_error.type == "error"
            assert early_error.error.code == "session_not_configured"

            websocket.send_json(
                {
                    "type": "session.update",
                    "event_id": "missing-model",
                    "session": {"type": "transcription"},
                }
            )
            model_error = receive_official_event(websocket)
            assert model_error.type == "error"
            assert model_error.error.code == "invalid_session"

            configure_transcription(websocket, event_id="retry-with-model")


@pytest.mark.parametrize(
    "query",
    [
        "model=gpt-4o-transcribe",
        "intent=realtime",
        "intent=transcription&model=gpt-4o-transcribe",
    ],
)
def test_realtime_rejects_non_transcription_handshakes(query: str) -> None:
    app = FastAPI()
    unexpected = _UnexpectedDataPlaneService()
    app.include_router(
        create_data_plane_router(
            realtime=unexpected,  # type: ignore[arg-type]
            webrtc=unexpected,  # type: ignore[arg-type]
            max_message_bytes=1024,
        )
    )
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/v1/realtime?{query}"):
                pass


def test_realtime_rejects_unsupported_x_aha_version() -> None:
    app = FastAPI()
    unexpected = _UnexpectedDataPlaneService()
    app.include_router(
        create_data_plane_router(
            realtime=unexpected,  # type: ignore[arg-type]
            webrtc=unexpected,  # type: ignore[arg-type]
            max_message_bytes=1024,
        )
    )
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as captured:
            with client.websocket_connect(
                "/v1/realtime?intent=transcription&x_aha=v2"
            ):
                pass
    assert captured.value.code == 4400


def test_realtime_server_vad_event_sequence_uses_sdk_models() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription&diagnostics=true",
            headers={"Authorization": "Bearer local-test-key"},
        ) as websocket:
            created = receive_official_event(websocket)
            assert created.type == "session.created"

            configure_transcription(websocket)

            audio = np.zeros(24_000, dtype="<i2").tobytes()
            websocket.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "event_id": "client-audio",
                    "audio": base64.b64encode(audio).decode("ascii"),
                }
            )
            events = [
                _SERVER_EVENT_ADAPTER.validate_python(
                    websocket.receive_json()
                )
                for _ in range(6)
            ]
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


def test_realtime_x_aha_reports_pending_semantic_turn() -> None:
    app = build_app(
        vad=TwoSegmentVad(),
        semantic_vad=PendingThenCompleteVad(),
    )
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription&x_aha=v1"
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            configure_transcription(websocket)
            audio = np.zeros(24_000, dtype="<i2").tobytes()
            websocket.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio).decode("ascii"),
                }
            )
            events = [websocket.receive_json() for _ in range(7)]

    assert [event["type"] for event in events] == [
        "input_audio_buffer.speech_started",
        "x_aha.vad.physical_speech_stopped",
        "input_audio_buffer.speech_stopped",
        "input_audio_buffer.committed",
        "conversation.item.created",
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.completed",
    ]
    extension = events[1]
    assert extension["turn_completed"] is False
    assert extension["audio_start_ms"] == 0
    assert extension["audio_end_ms"] > 0
    assert {
        event["item_id"]
        for event in events
        if "item_id" in event
    } == {events[0]["item_id"]}


def test_realtime_native_client_does_not_receive_x_aha_events() -> None:
    app = build_app(
        vad=TwoSegmentVad(),
        semantic_vad=PendingThenCompleteVad(),
    )
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            configure_transcription(websocket)
            audio = np.zeros(24_000, dtype="<i2").tobytes()
            websocket.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio).decode("ascii"),
                }
            )
            events = [receive_official_event(websocket) for _ in range(6)]

    assert all(not event.type.startswith("x_aha.") for event in events)
    assert events[0].type == "input_audio_buffer.speech_started"
    assert events[1].type == "input_audio_buffer.speech_stopped"


def test_realtime_rejects_manual_commit_without_closing_connection() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription",
            headers={"Authorization": "Bearer local-test-key"},
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            configure_transcription(websocket)
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
