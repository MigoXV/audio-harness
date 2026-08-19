from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from openai.types.realtime import RealtimeClientEvent, RealtimeServerEvent
from pydantic import TypeAdapter

from audio_harness.configs import WorkflowRuntimeConfig
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.serving.realtime.session import RealtimeTranscriptionSession
from audio_harness.serving.realtime.connection import (
    RealtimeConnectionMode,
    RealtimeTranscriptionService,
)
from audio_harness.serving.realtime import connection as realtime_connection
from audio_harness.serving.realtime.tools import ToolRegistry
from audio_harness.workflows.events import (
    SpeechStarted,
    SpeechStopped,
    VectorSearchResult,
)
from tests.test_agent_runtime import (
    BlockingResponses,
    ClientToolThenTextResponses,
    FakePool,
    _profile,
)
from tests.graph_fakes import static_audio_workflow


_CLIENT_EVENTS = TypeAdapter(RealtimeClientEvent)
_SERVER_EVENTS = TypeAdapter(RealtimeServerEvent)


class _Settings:
    def __init__(self, config: WorkflowRuntimeConfig | None = None) -> None:
        self.snapshot = SimpleNamespace(
            config=config or WorkflowRuntimeConfig()
        )

    def current(self):
        return self.snapshot


@pytest.mark.asyncio
async def test_realtime_session_uses_received_audio_cache_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    calls: list[tuple] = []

    class _Cache:
        def __init__(
            self,
            session_id: str,
            *,
            block_seconds: int,
            file_seconds: int,
            root,
        ) -> None:
            calls.append(
                ("create", session_id, block_seconds, file_seconds, root)
            )

        def append(self, audio: bytes) -> None:
            calls.append(("append", audio))

        async def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(
        "audio_harness.serving.realtime.session.ReceivedAudioCache",
        _Cache,
    )

    async def send_event(_event) -> None:
        return None

    session = RealtimeTranscriptionSession(
        send_event=send_event,
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(
            WorkflowRuntimeConfig(
                received_audio_cache_enabled=True,
                received_audio_cache_block_seconds=7,
                received_audio_cache_file_seconds=21,
            )
        ),  # type: ignore[arg-type]
        model="audio",
        target=_profile().audio_workflow,
        session_identifier="sess_cache",
        received_audio_cache_dir=tmp_path,
    )
    session._turn_detection = None

    await session.append_engine_audio(b"\x00\x00\x01\x00")
    await session.close()

    assert calls == [
        ("create", "sess_cache", 7, 21, tmp_path),
        ("append", b"\x00\x00\x01\x00"),
        ("close",),
    ]


class _AgentRoute:
    def __init__(self, target) -> None:
        self.target = target

    def resolve(self, model: str):
        if model != "voice-agent":
            raise RuntimeError("not found")
        return self.target


class _DisconnectedWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.closed: tuple[int, str] | None = None
        self.events: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    async def send_json(self, payload: dict) -> None:
        self.events.append(payload)

    async def receive(self):
        return {"type": "websocket.disconnect", "code": 1000, "reason": ""}


class _ScriptedWebSocket(_DisconnectedWebSocket):
    def __init__(self, messages: list[dict]) -> None:
        super().__init__()
        self.messages = list(messages)

    async def receive(self):
        if self.messages:
            return {
                "type": "websocket.receive",
                "text": json.dumps(self.messages.pop(0)),
            }
        return await super().receive()


@pytest.mark.asyncio
async def test_voice_agent_wss_handshake_emits_only_official_session_created() -> None:
    websocket = _DisconnectedWebSocket()
    service = RealtimeTranscriptionService(
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        routes=_AgentRoute(_profile()),  # type: ignore[arg-type]
        max_message_bytes=1024,
        inferencer_pool=FakePool(),  # type: ignore[arg-type]
    )

    await service.handle(
        websocket,  # type: ignore[arg-type]
        mode=RealtimeConnectionMode.REALTIME,
        model="voice-agent",
    )

    assert websocket.accepted is True
    assert [event["type"] for event in websocket.events] == ["session.created"]
    assert _SERVER_EVENTS.validate_python(websocket.events[0]).session.type == (
        "realtime"
    )


@pytest.mark.asyncio
async def test_speaker_filter_workflow_session_update_succeeds() -> None:
    _pool, _workflows, workflow = static_audio_workflow(
        asr=object(),
        vad=object(),
        semantic_vad=object(),
        speaker_filter=True,
    )
    websocket = _ScriptedWebSocket(
        [
            {
                "type": "session.update",
                "event_id": "update-filtered",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "transcription": {"model": "voice-agent"}
                        }
                    },
                },
            }
        ]
    )
    service = RealtimeTranscriptionService(
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        routes=_AgentRoute(workflow),  # type: ignore[arg-type]
        max_message_bytes=1024,
    )

    await service.handle(websocket)  # type: ignore[arg-type]

    assert [event["type"] for event in websocket.events] == [
        "session.created",
        "session.updated",
    ]
    assert all(_SERVER_EVENTS.validate_python(event) for event in websocket.events)


@pytest.mark.asyncio
async def test_dynamic_session_initialization_failure_is_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_session(*_args, **_kwargs):
        raise RuntimeError("invalid workflow")

    monkeypatch.setattr(
        realtime_connection._RealtimeConnection,
        "_new_session",
        fail_session,
    )
    websocket = _ScriptedWebSocket(
        [
            {
                "type": "session.update",
                "event_id": "update-invalid",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "transcription": {"model": "voice-agent"}
                        }
                    },
                },
            }
        ]
    )
    service = RealtimeTranscriptionService(
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        routes=_AgentRoute(_profile().audio_workflow),  # type: ignore[arg-type]
        max_message_bytes=1024,
    )

    await service.handle(websocket)  # type: ignore[arg-type]

    assert [event["type"] for event in websocket.events] == [
        "session.created",
        "error",
    ]
    error = websocket.events[-1]["error"]
    assert error["code"] == "model_workflow_invalid"
    assert error["event_id"] == "update-invalid"
    assert error["param"] == "session.audio.input.transcription.model"
    assert websocket.closed is None


@pytest.mark.asyncio
async def test_fixed_model_initialization_failure_closes_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_session(*_args, **_kwargs):
        raise RuntimeError("invalid workflow")

    monkeypatch.setattr(
        realtime_connection._RealtimeConnection,
        "_new_session",
        fail_session,
    )
    websocket = _DisconnectedWebSocket()
    service = RealtimeTranscriptionService(
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        routes=_AgentRoute(_profile()),  # type: ignore[arg-type]
        max_message_bytes=1024,
    )

    await service.handle(
        websocket,  # type: ignore[arg-type]
        mode=RealtimeConnectionMode.REALTIME,
        model="voice-agent",
    )

    assert [event["type"] for event in websocket.events] == ["error"]
    assert websocket.events[0]["error"]["code"] == "model_workflow_invalid"
    assert websocket.closed == (1011, "Model workflow invalid")


@pytest.mark.asyncio
async def test_realtime_model_rejects_plain_audio_workflow_before_accept() -> None:
    websocket = _DisconnectedWebSocket()
    profile = _profile()
    service = RealtimeTranscriptionService(
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        routes=_AgentRoute(profile.audio_workflow),  # type: ignore[arg-type]
        max_message_bytes=1024,
    )

    await service.handle(
        websocket,  # type: ignore[arg-type]
        mode=RealtimeConnectionMode.REALTIME,
        model="voice-agent",
    )

    assert websocket.accepted is False
    assert websocket.closed == (
        4400,
        "model must resolve to an active voice agent",
    )


@pytest.mark.asyncio
async def test_voice_agent_supports_complete_sdk_client_event_surface() -> None:
    emitted: list[dict] = []
    appended_audio: list[str] = []
    pool = FakePool()
    blocking = BlockingResponses(expected=1)
    pool.responses = blocking

    async def send_event(event) -> None:
        emitted.append(
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )

    async def append_audio(event) -> None:
        appended_audio.append(event.audio)

    session = RealtimeTranscriptionSession(
        send_event=send_event,
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        model="voice-agent",
        target=_profile(),
        audio_event_handler=append_audio,
        inferencer_pool=pool,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        transport="websocket",
    )

    async def send(payload: dict) -> None:
        await session.handle_client_event(
            _CLIENT_EVENTS.validate_python(payload)
        )

    await session.start()
    await send(
        {
            "type": "session.update",
            "event_id": "update",
            "session": {
                "type": "realtime",
                "output_modalities": ["text"],
            },
        }
    )
    await send(
        {
            "type": "conversation.item.create",
            "event_id": "create-user",
            "item": {
                "id": "user-1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        }
    )
    await send(
        {
            "type": "conversation.item.retrieve",
            "event_id": "retrieve-user",
            "item_id": "user-1",
        }
    )
    await send(
        {
            "type": "conversation.item.create",
            "event_id": "create-assistant",
            "previous_item_id": "user-1",
            "item": {
                "id": "assistant-audio",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_audio",
                        "audio": "AA==",
                        "transcript": "heard",
                    }
                ],
            },
        }
    )
    await send(
        {
            "type": "conversation.item.truncate",
            "event_id": "truncate",
            "item_id": "assistant-audio",
            "content_index": 0,
            "audio_end_ms": 0,
        }
    )
    encoded = base64.b64encode(b"\0\0" * 120).decode("ascii")
    await send(
        {
            "type": "input_audio_buffer.append",
            "event_id": "append",
            "audio": encoded,
        }
    )
    await send({"type": "input_audio_buffer.clear", "event_id": "clear"})
    await send({"type": "input_audio_buffer.commit", "event_id": "commit"})
    await send(
        {"type": "output_audio_buffer.clear", "event_id": "output-clear"}
    )
    await send(
        {
            "type": "response.create",
            "event_id": "response-create",
            "response": {
                "conversation": "none",
                "output_modalities": ["text"],
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "oob"}
                        ],
                    }
                ],
            },
        }
    )
    await asyncio.wait_for(blocking.started.wait(), timeout=1)
    response_id = next(
        event["response"]["id"]
        for event in reversed(emitted)
        if event["type"] == "response.created"
    )
    await send(
        {
            "type": "response.cancel",
            "event_id": "response-cancel",
            "response_id": response_id,
        }
    )
    await send(
        {
            "type": "conversation.item.delete",
            "event_id": "delete-user",
            "item_id": "user-1",
        }
    )
    await session.close()

    assert appended_audio == [encoded]
    assert {
        event["type"]
        for event in emitted
    } >= {
        "session.created",
        "session.updated",
        "conversation.item.added",
        "conversation.item.done",
        "conversation.item.retrieved",
        "conversation.item.truncated",
        "conversation.item.deleted",
        "input_audio_buffer.cleared",
        "response.created",
        "response.done",
        "error",
    }
    errors = {
        event["error"]["code"]
        for event in emitted
        if event["type"] == "error"
    }
    assert errors == {"input_audio_buffer_commit_empty", "invalid_value"}
    created_session = next(
        event["session"]
        for event in emitted
        if event["type"] == "session.created"
    )
    updated_session = next(
        event["session"]
        for event in emitted
        if event["type"] == "session.updated"
    )
    assert updated_session["id"] == created_session["id"]
    assert all(
        not event["type"].startswith("audio_harness.")
        for event in emitted
    )
    assert all(_SERVER_EVENTS.validate_python(event) for event in emitted)


@pytest.mark.asyncio
async def test_speech_started_interrupt_emits_turn_detected_reason() -> None:
    emitted: list[dict] = []
    cleared: list[int] = []
    pool = FakePool()
    blocking = BlockingResponses(expected=1)
    pool.responses = blocking

    async def send_event(event) -> None:
        emitted.append(
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )

    async def clear_audio(epoch: int) -> None:
        cleared.append(epoch)

    session = RealtimeTranscriptionSession(
        send_event=send_event,
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        model="voice-agent",
        target=_profile(),
        inferencer_pool=pool,  # type: ignore[arg-type]
        clear_audio=clear_audio,
        tool_registry=ToolRegistry(),
    )
    await session.start()
    await session.handle_client_event(
        _CLIENT_EVENTS.validate_python(
            {
                "type": "response.create",
                "event_id": "response-create",
                "response": {
                    "conversation": "auto",
                    "output_modalities": ["audio"],
                },
            }
        )
    )
    await asyncio.wait_for(blocking.started.wait(), timeout=1)

    await session._handle_workflow_event(
        "speech_started",
        SpeechStarted(start_sample=240, sample_rate=24_000),
    )
    await session.close()

    done = next(
        event for event in emitted if event["type"] == "response.done"
    )
    assert done["response"]["status"] == "cancelled"
    assert done["response"]["status_details"] == {
        "type": "cancelled",
        "reason": "turn_detected",
    }
    assert cleared
    assert emitted[-1]["type"] == "output_audio_buffer.cleared"
    assert all(_SERVER_EVENTS.validate_python(event) for event in emitted)


@pytest.mark.asyncio
async def test_voice_agent_x_aha_adds_pending_semantic_turn_event() -> None:
    emitted: list[dict] = []

    async def send_event(event) -> None:
        emitted.append(
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )

    session = RealtimeTranscriptionSession(
        send_event=send_event,
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        model="voice-agent",
        target=_profile(),
        inferencer_pool=FakePool(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        aha_version="v1",
    )
    await session.start()
    await session._handle_workflow_event(
        "speech_started",
        SpeechStarted(start_sample=240, sample_rate=24_000),
    )
    await session._handle_workflow_event(
        "turn_progress",
        AsrInferencer.Result(
            transcript="还没说完",
            is_final=False,
            start_sample=240,
            end_sample=12_000,
            sample_rate=24_000,
            turn_completed=False,
            language_code="zh-CN",
        ),
    )
    await session.close()

    assert [event["type"] for event in emitted] == [
        "session.created",
        "input_audio_buffer.speech_started",
        "x_aha.vad.physical_speech_stopped",
    ]
    extension = emitted[-1]
    assert extension["turn_completed"] is False
    assert extension["item_id"] == emitted[-2]["item_id"]
    assert all(
        _SERVER_EVENTS.validate_python(event)
        for event in emitted
        if not event["type"].startswith("x_aha.")
    )


@pytest.mark.asyncio
async def test_x_aha_vector_search_events_are_emitted_before_item_exists() -> None:
    emitted: list[dict] = []

    async def send_event(event) -> None:
        emitted.append(
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )

    session = RealtimeTranscriptionSession(
        send_event=send_event,
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        model="voice-agent",
        target=_profile(),
        inferencer_pool=FakePool(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        aha_version="v1",
    )
    await session.start()

    await session._handle_workflow_event(
        "vector_search",
        VectorSearchResult(
            start_sample=16_000,
            end_sample=32_000,
            sample_rate=16_000,
            collection_name="voiceprints",
            vector_name="eres2netv2",
            groups=(
                {
                    "speaker_id": "speaker-1",
                    "score": 0.91,
                    "observation": {
                        "point_id": "point-1",
                        "payload": {"speaker_id": "speaker-1"},
                    },
                },
            ),
            latency_ms=12.5,
        ),
    )
    await session._handle_workflow_event(
        "vector_search",
        VectorSearchResult(
            start_sample=32_000,
            end_sample=48_000,
            sample_rate=16_000,
            collection_name="voiceprints",
            vector_name="eres2netv2",
            groups=(),
            latency_ms=4.0,
            error_code="vector_search_failed",
            error_message="人物检索不可用",
        ),
    )
    await session.close()

    assert [event["type"] for event in emitted] == [
        "session.created",
        "x_aha.vector.search.completed",
        "x_aha.vector.search.failed",
    ]
    completed_event, failed_event = emitted[-2:]
    assert "item_id" not in completed_event
    assert completed_event["groups"][0]["speaker_id"] == "speaker-1"
    assert "item_id" not in failed_event
    assert failed_event["error"] == {
        "code": "vector_search_failed",
        "message": "人物检索不可用",
    }


@pytest.mark.asyncio
async def test_x_aha_turn_progress_starts_item_after_semantic_rollover() -> None:
    emitted: list[dict] = []

    async def send_event(event) -> None:
        emitted.append(
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )

    session = RealtimeTranscriptionSession(
        send_event=send_event,
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        model="voice-agent",
        target=_profile(),
        inferencer_pool=FakePool(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        aha_version="v1",
    )
    await session.start()
    await session._handle_workflow_event(
        "speech_started",
        SpeechStarted(start_sample=0, sample_rate=24_000),
    )
    first_item_id = emitted[-1]["item_id"]
    await session._handle_workflow_event(
        "speech_stopped",
        SpeechStopped(
            start_sample=0,
            end_sample=24_000,
            sample_rate=24_000,
        ),
    )
    await session._handle_workflow_event(
        "turn_progress",
        AsrInferencer.Result(
            transcript="",
            is_final=True,
            start_sample=24_000,
            end_sample=36_000,
            sample_rate=24_000,
            turn_completed=False,
        ),
    )
    await session.close()

    recovered_started, progress = emitted[-2:]
    assert recovered_started["type"] == "input_audio_buffer.speech_started"
    assert recovered_started["audio_start_ms"] == 1_000
    assert progress["type"] == "x_aha.vad.physical_speech_stopped"
    assert progress["item_id"] == recovered_started["item_id"]
    assert progress["item_id"] != first_item_id
    assert all(
        _SERVER_EVENTS.validate_python(event)
        for event in emitted
        if not event["type"].startswith("x_aha.")
    )


@pytest.mark.asyncio
async def test_semantic_vad_promotes_the_unconsumed_physical_start() -> None:
    emitted: list[dict] = []

    async def send_event(event) -> None:
        emitted.append(
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )

    session = RealtimeTranscriptionSession(
        send_event=send_event,
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        model="voice-agent",
        target=_profile(),
        inferencer_pool=FakePool(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        aha_version="v1",
    )
    await session.start()

    async def started(start_sample: int) -> None:
        await session._handle_workflow_event(
            "speech_started",
            SpeechStarted(start_sample=start_sample, sample_rate=1_000),
        )

    async def stopped(
        start_sample: int,
        end_sample: int,
        last_segment_start_sample: int,
    ) -> None:
        await session._handle_workflow_event(
            "speech_stopped",
            SpeechStopped(
                start_sample=start_sample,
                end_sample=end_sample,
                sample_rate=1_000,
                last_segment_start_sample=last_segment_start_sample,
            ),
        )

    await started(0)
    await started(19_700)
    await started(39_700)
    await stopped(0, 40_280, 19_700)

    await started(59_700)
    await started(79_700)
    await stopped(39_700, 80_280, 59_700)

    await started(99_700)
    await started(119_760)
    await stopped(79_700, 120_340, 99_700)

    await started(140_100)
    await stopped(119_760, 140_390, 119_760)
    await session.close()

    speech_started = [
        event
        for event in emitted
        if event["type"] == "input_audio_buffer.speech_started"
    ]
    speech_stopped = [
        event
        for event in emitted
        if event["type"] == "input_audio_buffer.speech_stopped"
    ]
    assert [event["audio_start_ms"] for event in speech_started] == [
        0,
        39_700,
        79_700,
        119_760,
        140_100,
    ]
    assert [event["audio_end_ms"] for event in speech_stopped] == [
        40_280,
        80_280,
        120_340,
        140_390,
    ]
    assert [event["item_id"] for event in speech_started[:4]] == [
        event["item_id"] for event in speech_stopped
    ]
    assert all(
        _SERVER_EVENTS.validate_python(event)
        for event in emitted
        if not event["type"].startswith("x_aha.")
    )


@pytest.mark.asyncio
async def test_speech_stopped_starts_item_when_start_event_is_missing() -> None:
    emitted: list[dict] = []

    async def send_event(event) -> None:
        emitted.append(
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )

    session = RealtimeTranscriptionSession(
        send_event=send_event,
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        model="voice-agent",
        target=_profile(),
        inferencer_pool=FakePool(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        aha_version="v1",
    )
    await session.start()
    await session._handle_workflow_event(
        "speech_stopped",
        SpeechStopped(
            start_sample=24_000,
            end_sample=48_000,
            sample_rate=24_000,
        ),
    )
    await session.close()

    assert [event["type"] for event in emitted] == [
        "session.created",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "input_audio_buffer.committed",
        "conversation.item.added",
        "conversation.item.done",
    ]
    item_ids = {
        event["item_id"] for event in emitted if "item_id" in event
    }
    assert len(item_ids) == 1
    assert emitted[1]["audio_start_ms"] == 1_000
    assert all(_SERVER_EVENTS.validate_python(event) for event in emitted)


@pytest.mark.asyncio
async def test_x_aha_interim_transcript_is_cumulative_and_cleared_by_primary() -> None:
    emitted: list[dict] = []

    async def send_event(event) -> None:
        emitted.append(
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )

    session = RealtimeTranscriptionSession(
        send_event=send_event,
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        model="voice-agent",
        target=_profile(),
        inferencer_pool=FakePool(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        aha_version="v1",
    )
    await session.start()
    await session._handle_workflow_event(
        "speech_started",
        SpeechStarted(start_sample=0, sample_rate=16_000),
    )
    for transcript, start, end, final in (
        ("hello", 0, 8_000, False),
        ("world", 8_000, 16_000, True),
    ):
        await session._handle_workflow_event(
            "interim_transcript",
            AsrInferencer.Result(
                transcript=transcript,
                is_final=final,
                start_sample=start,
                end_sample=end,
                sample_rate=16_000,
                turn_completed=True,
            ),
        )
    await session._handle_workflow_event(
        "speech_stopped",
        SpeechStopped(
            start_sample=0,
            end_sample=16_000,
            sample_rate=16_000,
        ),
    )
    await session._handle_workflow_event(
        "primary_transcript",
        AsrInferencer.Result(
            transcript="hello world",
            is_final=True,
            start_sample=0,
            end_sample=16_000,
            sample_rate=16_000,
            turn_completed=True,
        ),
    )
    await session.close()

    interim = [
        event
        for event in emitted
        if event["type"] == "x_aha.input_audio_transcription.interim"
    ]
    assert [(event["revision"], event["transcript"]) for event in interim] == [
        (1, "hello"),
        (2, "hello world"),
    ]
    cleared_index = next(
        index
        for index, event in enumerate(emitted)
        if event["type"]
        == "x_aha.input_audio_transcription.interim_cleared"
    )
    delta_index = next(
        index
        for index, event in enumerate(emitted)
        if event["type"]
        == "conversation.item.input_audio_transcription.delta"
    )
    assert cleared_index < delta_index
    assert emitted[cleared_index]["reason"] == "primary_transcript_started"
    assert len({event["item_id"] for event in emitted if "item_id" in event}) == 1


@pytest.mark.asyncio
async def test_oob_client_tool_output_does_not_enter_default_conversation() -> None:
    emitted: list[dict] = []
    pool = FakePool()
    pool.responses = ClientToolThenTextResponses()

    async def send_event(event) -> None:
        emitted.append(
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )

    session = RealtimeTranscriptionSession(
        send_event=send_event,
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        model="voice-agent",
        target=_profile(),
        inferencer_pool=pool,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
    )
    await session.handle_client_event(
        _CLIENT_EVENTS.validate_python(
            {
                "type": "response.create",
                "response": {
                    "conversation": "none",
                    "input": [],
                    "output_modalities": ["text"],
                    "tools": [
                        {
                            "type": "function",
                            "name": "client_tool",
                            "description": "client tool",
                            "parameters": {"type": "object"},
                        }
                    ],
                },
            }
        )
    )
    assert session._agent is not None

    async def wait_for_pending_tool() -> None:
        while not session._agent._pending_client_tools:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_pending_tool(), timeout=1)
    await session.handle_client_event(
        _CLIENT_EVENTS.validate_python(
            {
                "type": "conversation.item.create",
                "event_id": "oob-tool-result",
                "item": {
                    "id": "item_oob_tool_result",
                    "type": "function_call_output",
                    "call_id": "call_client_tool",
                    "output": "ok",
                },
            }
        )
    )
    task = session._agent._task
    assert task is not None
    await asyncio.wait_for(task, timeout=1)
    await session.close()

    assert session._agent._history == []
    assert all(
        event.get("item", {}).get("id") != "item_oob_tool_result"
        for event in emitted
    )
    assert all(_SERVER_EVENTS.validate_python(event) for event in emitted)


@pytest.mark.asyncio
async def test_webrtc_output_audio_clear_executes_and_emits_official_event() -> None:
    emitted: list[dict] = []
    cleared: list[int] = []
    pool = FakePool()
    blocking = BlockingResponses(expected=1)
    pool.responses = blocking

    async def send_event(event) -> None:
        emitted.append(
            event.model_dump(mode="json", exclude_none=True)
            if hasattr(event, "model_dump")
            else event
        )

    async def send_audio(_pcm: bytes, _rate: int, _epoch: int) -> None:
        return None

    async def clear_audio(epoch: int) -> None:
        cleared.append(epoch)

    session = RealtimeTranscriptionSession(
        send_event=send_event,
        session_service=object(),  # type: ignore[arg-type]
        workflow_settings=_Settings(),  # type: ignore[arg-type]
        model="voice-agent",
        target=_profile(),
        inferencer_pool=pool,  # type: ignore[arg-type]
        send_audio=send_audio,
        clear_audio=clear_audio,
        tool_registry=ToolRegistry(),
        transport="webrtc",
    )
    await session.handle_client_event(
        _CLIENT_EVENTS.validate_python(
            {
                "type": "response.create",
                "response": {
                    "conversation": "none",
                    "output_modalities": ["audio"],
                    "input": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "hello"}
                            ],
                        }
                    ],
                },
            }
        )
    )
    await asyncio.wait_for(blocking.started.wait(), timeout=1)
    response_id = next(
        event["response"]["id"]
        for event in emitted
        if event["type"] == "response.created"
    )

    await session.handle_client_event(
        _CLIENT_EVENTS.validate_python(
            {
                "type": "response.cancel",
                "event_id": "cancel",
                "response_id": response_id,
            }
        )
    )
    await session.handle_client_event(
        _CLIENT_EVENTS.validate_python(
            {"type": "output_audio_buffer.clear", "event_id": "clear-output"}
        )
    )
    await session.close()

    assert cleared
    cleared_event = next(
        event
        for event in emitted
        if event["type"] == "output_audio_buffer.cleared"
    )
    assert cleared_event["response_id"] == response_id
    assert _SERVER_EVENTS.validate_python(cleared_event)


@pytest.mark.asyncio
async def test_workflow_cleanup_survives_transcription_finish_failure() -> None:
    task_cancelled = asyncio.Event()

    async def run_workflow() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            task_cancelled.set()

    class FailingTranscription:
        async def finish(self) -> None:
            raise RuntimeError("finish failed")

    class SessionContext:
        def __init__(self) -> None:
            self.exited = False

        async def __aexit__(self, *_args) -> None:
            self.exited = True

    context = SessionContext()
    session = object.__new__(RealtimeTranscriptionSession)
    session._workflow_task = asyncio.create_task(run_workflow())
    session._transcription = FailingTranscription()
    session._session_context = context
    session._reservation = None
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="finish failed"):
        await session._finish_workflow(send_results=False)

    assert task_cancelled.is_set()
    assert context.exited is True
    assert session._workflow_task is None
    assert session._transcription is None
    assert session._session_context is None
