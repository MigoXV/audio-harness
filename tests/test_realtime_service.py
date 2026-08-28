from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI, Query, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from audio_harness.serving.capacity import ServingCapacity
from audio_harness.configs import CapacityConfig, WorkflowRuntimeConfig
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.serving.realtime.connection import (
    RealtimeConnectionMode,
    RealtimeTranscriptionService,
    _OnlinePcmResampler,
)
from audio_harness.serving.realtime.protocol import (
    AHA_REALTIME_SERVER_EVENT_ADAPTER,
)
from audio_harness.api.routers.data_plane import create_data_plane_router
from audio_harness.sessions.audio_workflow import AudioWorkflowSessionService
from audio_harness.workflows.settings import WorkflowSettingsSnapshot
from tests.graph_fakes import StaticRouteService, static_audio_workflow

_SERVER_EVENT_ADAPTER = AHA_REALTIME_SERVER_EVENT_ADAPTER


class ImmediateVad(VadInferencer):
    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadInferencer.Update]:
        async for chunk in audio_chunks:
            samples = len(chunk) // 2
            yield VadInferencer.Update(
                boundaries=((0, samples),),
                processed_samples=samples,
            )
            return

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        return True

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass


class RecordingVad(ImmediateVad):
    def __init__(self) -> None:
        self.chunk_sizes: list[int] = []

    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadInferencer.Update]:
        processed_samples = 0
        async for chunk in audio_chunks:
            self.chunk_sizes.append(len(chunk))
            processed_samples += len(chunk) // 2
        yield VadInferencer.Update(
            boundaries=((0, processed_samples),),
            processed_samples=processed_samples,
        )


class TwoSegmentVad(ImmediateVad):
    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadInferencer.Update]:
        async for chunk in audio_chunks:
            samples = len(chunk) // 2
            middle = samples // 2
            yield VadInferencer.Update(
                boundaries=(
                    (0, middle),
                    (middle, samples),
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


class FinalAsr(AsrInferencer):
    async def transcribe(
        self,
        audio: bytes,
        *,
        start_sample: int,
        end_sample: int,
        turn_completed: bool,
        options: AsrInferencer.Options,
    ) -> AsyncIterator[AsrInferencer.Result]:
        yield AsrInferencer.Result(
            transcript="你好，世界",
            is_final=True,
            start_sample=start_sample,
            end_sample=end_sample,
            sample_rate=options.sample_rate,
            turn_completed=turn_completed,
            language_code=options.language_code,
        )

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass


class StaticSettings:
    def __init__(self, config: WorkflowRuntimeConfig) -> None:
        self._snapshot = WorkflowSettingsSnapshot(
            config=config,
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
    inferencer_pool, _workflows, workflow = static_audio_workflow(
        asr=FinalAsr(),
        vad=selected_vad,
        semantic_vad=semantic_vad or selected_vad,
    )
    sessions = AudioWorkflowSessionService(
        inferencer_pool=inferencer_pool,
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


def configure_transcription(
    websocket,
    *,
    event_id: str = "client-session",
    sample_rate: int = 24_000,
):
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
                            "rate": sample_rate,
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
    assert updated.session.id is not None
    return updated


def test_realtime_accepts_16_khz_transcription_session() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            updated = configure_transcription(websocket, sample_rate=16_000)

            assert updated.session.audio.input.format.rate == 16_000


@pytest.mark.parametrize("format_type", ["audio/pcmu", "audio/pcma"])
def test_realtime_rejects_g711_and_keeps_connection_open(
    format_type: str,
) -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            websocket.send_json(
                {
                    "type": "session.update",
                    "event_id": "unsupported-audio-format",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "format": {"type": format_type},
                                "transcription": {
                                    "model": "gpt-4o-transcribe"
                                },
                            }
                        },
                    },
                }
            )

            error = receive_official_event(websocket)
            assert error.type == "error"
            assert error.error.code == "invalid_session"
            assert "不支持的输入音频格式" in error.error.message
            configure_transcription(websocket, event_id="pcm-after-error")


class _UnexpectedDataPlaneService:
    async def handle(self, *args, **kwargs) -> None:
        raise AssertionError("不支持的 x_aha 版本不应进入 Realtime 服务")

    async def create_call(self, *args, **kwargs):
        raise AssertionError("不支持的 x_aha 版本不应进入 WebRTC 服务")

    async def hangup(self, call_id: str) -> None:
        raise AssertionError("不应调用 hangup")


class _RecordingRealtimeService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def handle(self, websocket: WebSocket, **kwargs) -> None:
        self.calls.append(kwargs)
        await websocket.accept()
        await websocket.close()


class _RecordingWebRtcService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_call(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(call_id="call-1", sdp="answer-sdp")

    async def hangup(self, call_id: str) -> None:
        del call_id


def test_webrtc_session_form_accepts_16_khz_input() -> None:
    app = FastAPI()
    webrtc = _RecordingWebRtcService()
    app.include_router(
        create_data_plane_router(
            realtime=_UnexpectedDataPlaneService(),  # type: ignore[arg-type]
            webrtc=webrtc,  # type: ignore[arg-type]
            max_message_bytes=4096,
        )
    )
    session = {
        "type": "transcription",
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 16_000},
                "transcription": {"model": "meeting-transcribe"},
            }
        },
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/realtime/calls",
            files={
                "sdp": (None, "offer-sdp"),
                "session": (None, json.dumps(session)),
            },
        )

    assert response.status_code == 201
    configured = webrtc.calls[0]["session"]
    assert configured.audio.input.format.rate == 16_000


def test_realtime_does_not_require_bearer_token() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            created = receive_official_event(websocket)
            assert created.type == "session.created"
            assert created.session.type == "transcription"


def test_realtime_model_handshake_selects_ordinary_realtime_mode() -> None:
    app = FastAPI()
    realtime = _RecordingRealtimeService()
    app.include_router(
        create_data_plane_router(
            realtime=realtime,  # type: ignore[arg-type]
            webrtc=_UnexpectedDataPlaneService(),  # type: ignore[arg-type]
            max_message_bytes=1024,
        )
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/realtime?model=public-voice-agent"
        ) as websocket:
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_json()

    assert realtime.calls == [
        {
            "mode": RealtimeConnectionMode.REALTIME,
            "model": "public-voice-agent",
            "diagnostics_enabled": False,
            "aha_version": None,
        }
    ]


def test_realtime_model_handshake_forwards_explicit_x_aha() -> None:
    app = FastAPI()
    realtime = _RecordingRealtimeService()
    app.include_router(
        create_data_plane_router(
            realtime=realtime,  # type: ignore[arg-type]
            webrtc=_UnexpectedDataPlaneService(),  # type: ignore[arg-type]
            max_message_bytes=1024,
        )
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/realtime?model=public-voice-agent&x_aha=v1"
        ) as websocket:
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_json()

    assert realtime.calls == [
        {
            "mode": RealtimeConnectionMode.REALTIME,
            "model": "public-voice-agent",
            "diagnostics_enabled": False,
            "aha_version": "v1",
        }
    ]


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


def test_realtime_malformed_json_returns_error_and_keeps_connection_open() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            websocket.send_text("{")

            error = receive_official_event(websocket)
            assert error.type == "error"
            assert error.error.code == "invalid_event"
            configure_transcription(websocket)


def test_realtime_rejects_session_update_with_resampler_audio_pending() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            configure_transcription(websocket)
            websocket.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(b"\0\0").decode("ascii"),
                }
            )
            websocket.send_json(
                {
                    "type": "session.update",
                    "event_id": "update-with-pending-audio",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "transcription": {"model": "other-model"},
                                "format": {
                                    "type": "audio/pcm",
                                    "rate": 24_000,
                                },
                            }
                        },
                    },
                }
            )

            error = receive_official_event(websocket)
            assert error.type == "error"
            assert error.error.code == "session_update_after_audio"
            websocket.send_json({"type": "input_audio_buffer.clear"})
            assert receive_official_event(websocket).type == (
                "input_audio_buffer.cleared"
            )


def test_realtime_rejects_sample_rate_change_with_audio_pending() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            configure_transcription(websocket)
            websocket.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(b"\0\0").decode("ascii"),
                }
            )
            websocket.send_json(
                {
                    "type": "session.update",
                    "event_id": "change-rate-with-pending-audio",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "transcription": {
                                    "model": "gpt-4o-transcribe"
                                },
                                "format": {
                                    "type": "audio/pcm",
                                    "rate": 16_000,
                                },
                            }
                        },
                    },
                }
            )

            error = receive_official_event(websocket)
            assert error.type == "error"
            assert error.error.code == "session_update_after_audio"
            websocket.send_json({"type": "input_audio_buffer.clear"})
            assert receive_official_event(websocket).type == (
                "input_audio_buffer.cleared"
            )
            updated = configure_transcription(
                websocket,
                event_id="change-rate-after-clear",
                sample_rate=16_000,
            )
            assert updated.session.audio.input.format.rate == 16_000


def test_realtime_allows_safe_session_update_with_resampler_audio_pending() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            configure_transcription(websocket)
            websocket.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(b"\0\0").decode("ascii"),
                }
            )
            websocket.send_json(
                {
                    "type": "session.update",
                    "event_id": "safe-update-with-pending-audio",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "transcription": {
                                    "model": "gpt-4o-transcribe",
                                    "language": "en-US",
                                },
                                "format": {
                                    "type": "audio/pcm",
                                    "rate": 24_000,
                                },
                            }
                        },
                    },
                }
            )

            updated = receive_official_event(websocket)
            assert updated.type == "session.updated"
            assert updated.session.audio.input.transcription.language == "en-US"
            websocket.send_json({"type": "input_audio_buffer.clear"})
            assert receive_official_event(websocket).type == (
                "input_audio_buffer.cleared"
            )


@pytest.mark.parametrize(
    "query",
    [
        "",
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
                for _ in range(7)
            ]
            assert [event.type for event in events] == [
                "input_audio_buffer.speech_started",
                "input_audio_buffer.speech_stopped",
                "input_audio_buffer.committed",
                "conversation.item.added",
                "conversation.item.done",
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
            websocket.send_json(
                {
                    "type": "session.update",
                    "event_id": "safe-update-after-server-vad",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "format": {
                                    "type": "audio/pcm",
                                    "rate": 24_000,
                                },
                                "transcription": {
                                    "model": "gpt-4o-transcribe",
                                    "language": "en-US",
                                },
                            }
                        },
                    },
                }
            )
            updated = receive_official_event(websocket)
            assert updated.type == "session.updated"
            assert updated.session.audio.input.transcription.language == "en-US"


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
            events = [websocket.receive_json() for _ in range(9)]

    assert [event["type"] for event in events] == [
        "input_audio_buffer.speech_started",
        "x_aha.vad.progress",
        "x_aha.vad.physical_speech_stopped",
        "input_audio_buffer.speech_stopped",
        "input_audio_buffer.committed",
        "conversation.item.added",
        "conversation.item.done",
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.completed",
    ]
    progress = events[1]
    assert progress["processed_audio_ms"] == 200
    assert "final" not in progress
    extension = events[2]
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
            events = [receive_official_event(websocket) for _ in range(7)]

    assert all(not event.type.startswith("x_aha.") for event in events)
    assert events[0].type == "input_audio_buffer.speech_started"
    assert events[1].type == "input_audio_buffer.speech_stopped"


def test_realtime_rejects_empty_manual_commit_without_closing_connection() -> None:
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
            assert error.error.code == "input_audio_buffer_commit_empty"

            websocket.send_json({"type": "input_audio_buffer.clear"})
            assert (
                receive_official_event(websocket).type
                == "input_audio_buffer.cleared"
            )


def test_realtime_manual_commit_finishes_buffer_when_vad_is_disabled() -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            websocket.send_json(
                {
                    "type": "session.update",
                    "event_id": "manual-session",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "format": {
                                    "type": "audio/pcm",
                                    "rate": 24_000,
                                },
                                "transcription": {
                                    "model": "gpt-4o-transcribe",
                                    "language": "zh-CN",
                                },
                                "turn_detection": None,
                            }
                        },
                    },
                }
            )
            updated = receive_official_event(websocket)
            assert updated.type == "session.updated"
            assert updated.session.audio.input.turn_detection is None

            audio = np.zeros(24_000, dtype="<i2").tobytes()
            websocket.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio).decode("ascii"),
                }
            )
            websocket.send_json(
                {
                    "type": "input_audio_buffer.commit",
                    "event_id": "manual-commit",
                }
            )
            events = [receive_official_event(websocket) for _ in range(5)]

    assert [event.type for event in events] == [
        "input_audio_buffer.committed",
        "conversation.item.added",
        "conversation.item.done",
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.completed",
    ]


def test_realtime_manual_commit_chunks_audio_larger_than_grpc_limit() -> None:
    vad = RecordingVad()
    with TestClient(build_app(vad=vad)) as client:
        with client.websocket_connect(
            "/v1/realtime?intent=transcription"
        ) as websocket:
            assert receive_official_event(websocket).type == "session.created"
            websocket.send_json(
                {
                    "type": "session.update",
                    "event_id": "manual-session",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "format": {
                                    "type": "audio/pcm",
                                    "rate": 24_000,
                                },
                                "transcription": {
                                    "model": "gpt-4o-transcribe",
                                    "language": "zh-CN",
                                },
                                "turn_detection": None,
                            }
                        },
                    },
                }
            )
            assert receive_official_event(websocket).type == "session.updated"

            input_audio = np.zeros(24_000 * 132, dtype="<i2").tobytes()
            input_chunk_bytes = 24_000 * 2
            for offset in range(0, len(input_audio), input_chunk_bytes):
                websocket.send_json(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(
                            input_audio[offset : offset + input_chunk_bytes]
                        ).decode("ascii"),
                    }
                )
            websocket.send_json(
                {
                    "type": "input_audio_buffer.commit",
                    "event_id": "manual-commit",
                }
            )
            events = [receive_official_event(websocket) for _ in range(5)]

    assert events[-1].type == (
        "conversation.item.input_audio_transcription.completed"
    )
    assert sum(vad.chunk_sizes) > 4 * 1024 * 1024
    assert len(vad.chunk_sizes) > 1
    assert max(vad.chunk_sizes) <= 6400


@pytest.mark.parametrize("input_sample_rate", [16_000, 24_000])
def test_online_pcm_processing_preserves_one_second_duration(
    input_sample_rate: int,
) -> None:
    resampler = _OnlinePcmResampler(
        chunk_bytes=6400,
        input_sample_rate=input_sample_rate,
    )
    chunks: list[bytes] = []
    input_audio = np.arange(input_sample_rate, dtype=np.int16).tobytes()
    for offset in range(0, len(input_audio), 4800):
        chunks.extend(resampler.append(input_audio[offset : offset + 4800]))
    chunks.extend(resampler.finish())

    assert sum(map(len, chunks)) // 2 == 16_000
    assert all(len(chunk) <= 6400 for chunk in chunks)


def test_online_resampler_rejects_incomplete_pcm_sample() -> None:
    resampler = _OnlinePcmResampler(chunk_bytes=6400)

    with pytest.raises(ValueError, match="PCM S16LE"):
        resampler.append(b"\0")
