from __future__ import annotations

import pytest
from openai.types.realtime import (
    InputAudioBufferAppendEvent,
    SessionCreatedEvent,
    SessionUpdateEvent,
    SessionUpdatedEvent,
)
from pydantic import ValidationError

from audio_harness.serving.realtime.protocol import (
    AHA_REALTIME_CLIENT_EVENT_ADAPTER,
    AHA_REALTIME_SERVER_EVENT_ADAPTER,
    AhaAudioPCM,
    AhaSessionCreatedEvent,
    AhaSessionUpdateEvent,
    AhaSessionUpdatedEvent,
    AhaV1ServerEventBase,
    AhaAgentBackendApprovalResponseEvent,
    AhaAgentBackendQuestionResponseEvent,
)


def _session_event(event_type: str, rate: int) -> dict:
    payload = {
        "type": event_type,
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": rate},
                    "transcription": {"model": "meeting-transcribe"},
                }
            },
        },
    }
    payload["event_id"] = "event-1"
    return payload


@pytest.mark.parametrize("rate", [16_000, 24_000])
def test_extended_session_events_accept_supported_pcm_rates(rate: int) -> None:
    updated = AHA_REALTIME_CLIENT_EVENT_ADAPTER.validate_python(
        _session_event("session.update", rate)
    )
    created = AHA_REALTIME_SERVER_EVENT_ADAPTER.validate_python(
        _session_event("session.created", rate)
    )
    server_updated = AHA_REALTIME_SERVER_EVENT_ADAPTER.validate_python(
        _session_event("session.updated", rate)
    )

    assert isinstance(updated, AhaSessionUpdateEvent)
    assert isinstance(updated, SessionUpdateEvent)
    assert isinstance(created, AhaSessionCreatedEvent)
    assert isinstance(created, SessionCreatedEvent)
    assert isinstance(server_updated, AhaSessionUpdatedEvent)
    assert isinstance(server_updated, SessionUpdatedEvent)
    assert isinstance(updated.session.audio.input.format, AhaAudioPCM)
    assert updated.session.audio.input.format.rate == rate


def test_extended_session_event_rejects_unsupported_pcm_rate() -> None:
    with pytest.raises(ValidationError):
        AHA_REALTIME_CLIENT_EVENT_ADAPTER.validate_python(
            _session_event("session.update", 8_000)
        )


def test_standard_client_events_still_use_openai_models() -> None:
    event = AHA_REALTIME_CLIENT_EVENT_ADAPTER.validate_python(
        {"type": "input_audio_buffer.append", "audio": "AAA="}
    )

    assert type(event) is InputAudioBufferAppendEvent


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "x_aha.vad.progress",
            "event_id": "e",
            "processed_audio_ms": 100,
        },
        {
            "type": "x_aha.vad.physical_speech_started",
            "event_id": "e",
            "vad_turn_id": "vturn_1",
            "physical_segment_id": "pseg_1",
            "item_id": "i",
            "audio_start_ms": 0,
        },
        {
            "type": "x_aha.vad.physical_speech_stopped",
            "event_id": "e",
            "vad_turn_id": "vturn_1",
            "physical_segment_id": "pseg_1",
            "item_id": "i",
            "audio_start_ms": 0,
            "audio_end_ms": 10,
        },
        {
            "type": "x_aha.vad.turn_completed",
            "event_id": "e",
            "vad_turn_id": "vturn_1",
            "item_id": "i",
            "audio_start_ms": 0,
            "audio_end_ms": 10,
            "physical_segment_ids": ["pseg_1"],
        },
        {
            "type": "x_aha.input_audio_transcription.interim",
            "event_id": "e",
            "item_id": "i",
            "content_index": 0,
            "revision": 1,
            "transcript": "hi",
            "audio_start_ms": 0,
            "audio_end_ms": 10,
        },
        {
            "type": "x_aha.input_audio_transcription.interim_cleared",
            "event_id": "e",
            "item_id": "i",
            "reason": "superseded",
        },
        {
            "type": "x_aha.vector.search.completed",
            "event_id": "e",
            "item_id": "i",
            "audio_start_ms": 0,
            "audio_end_ms": 10,
            "collection_name": "c",
            "vector_name": "v",
            "groups": [],
            "latency_ms": 1.0,
        },
        {
            "type": "x_aha.vector.search.completed",
            "event_id": "e",
            "audio_start_ms": 0,
            "audio_end_ms": 10,
            "collection_name": "c",
            "vector_name": "v",
            "groups": [],
            "latency_ms": 1.0,
        },
        {
            "type": "x_aha.vector.search.failed",
            "event_id": "e",
            "item_id": "i",
            "audio_start_ms": 0,
            "audio_end_ms": 10,
            "collection_name": "c",
            "vector_name": "v",
            "error": {"code": "failed", "message": "failed"},
        },
        {
            "type": "x_aha.vector.search.failed",
            "event_id": "e",
            "audio_start_ms": 0,
            "audio_end_ms": 10,
            "collection_name": "c",
            "vector_name": "v",
            "error": {"code": "failed", "message": "failed"},
        },
        *[
            {
                "type": f"x_aha.diarization.local.{phase}",
                "event_id": "e",
                "batch_id": "b",
                "batch_audio_start_ms": 0,
                "batch_audio_end_ms": 10,
                "speech_duration_ms": 10,
                "segments": [],
                "latency_ms": 1.0,
                **(
                    {"error": {"code": "failed", "message": "failed"}}
                    if phase == "failed"
                    else {}
                ),
            }
            for phase in ("delta", "completed", "failed")
        ],
        *[
            {
                "type": (
                    "x_aha.input_audio_transcription.speaker." + phase
                ),
                "event_id": "e",
                "item_id": "i",
                "item_source_id": "source",
                "segment_id": "segment",
                "batch_id": "batch",
                "local_speaker_id": "batch:1",
                "speaker": 1,
                "audio_start_ms": 0,
                "audio_end_ms": 10,
                **(
                    {"delta": "hi"}
                    if phase == "delta"
                    else {"transcript": "hi"}
                    if phase == "completed"
                    else {"error": {"code": "failed", "message": "failed"}}
                ),
            }
            for phase in ("delta", "completed", "failed")
        ],
        {
            "type": "x_aha.diarization.cluster.completed",
            "event_id": "e",
            "revision": 1,
            "replaces_revision": None,
            "full_snapshot": True,
            "assignments": [],
        },
        {
            "type": "x_aha.diarization.cluster.failed",
            "event_id": "e",
            "revision": 1,
            "replaces_revision": None,
            "error": {"code": "failed", "message": "failed"},
        },
        {
            "type": "x_aha.agent_backend.session",
            "event_id": "e",
            "backend_id": "b",
            "backend_session_id": "s",
            "status": "running",
        },
        {
            "type": "x_aha.agent_backend.tool.started",
            "event_id": "e",
            "backend_id": "b",
            "backend_session_id": "s",
            "seq": 1,
            "call_id": "c",
            "name": "bash",
            "arguments": "{}",
        },
        {
            "type": "x_aha.agent_backend.approval.requested",
            "event_id": "e",
            "backend_id": "b",
            "backend_session_id": "s",
            "request_id": "r",
            "approval_id": "a",
            "tool_name": "bash",
        },
        {
            "type": "x_aha.agent_backend.question.requested",
            "event_id": "e",
            "backend_id": "b",
            "backend_session_id": "s",
            "request_id": "r",
            "questions": [],
        },
        {
            "type": "x_aha.agent_backend.failed",
            "event_id": "e",
            "backend_id": "b",
            "backend_session_id": "s",
            "code": "offline",
            "message": "offline",
        },
    ],
)
def test_all_aha_v1_events_are_part_of_server_union(payload: dict) -> None:
    event = AHA_REALTIME_SERVER_EVENT_ADAPTER.validate_python(payload)

    assert isinstance(event, AhaV1ServerEventBase)


def test_aha_v1_events_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AHA_REALTIME_SERVER_EVENT_ADAPTER.validate_python(
            {
                "type": "x_aha.vad.progress",
                "event_id": "e",
                "processed_audio_ms": 100,
                "typo": True,
            }
        )


def test_aha_v1_rejects_legacy_physical_stop_turn_completed() -> None:
    with pytest.raises(ValidationError):
        AHA_REALTIME_SERVER_EVENT_ADAPTER.validate_python(
            {
                "type": "x_aha.vad.physical_speech_stopped",
                "event_id": "e",
                "vad_turn_id": "vturn_1",
                "physical_segment_id": "pseg_1",
                "audio_start_ms": 0,
                "audio_end_ms": 10,
                "turn_completed": False,
            }
        )


def test_agent_backend_control_events_are_part_of_client_union() -> None:
    approval = AHA_REALTIME_CLIENT_EVENT_ADAPTER.validate_python(
        {
            "type": "x_aha.agent_backend.approval.respond",
            "event_id": "e",
            "request_id": "r",
            "approval_id": "a",
            "approved": True,
        }
    )
    question = AHA_REALTIME_CLIENT_EVENT_ADAPTER.validate_python(
        {
            "type": "x_aha.agent_backend.question.respond",
            "request_id": "r",
            "answers": [
                {"id": "q", "selected": ["one"], "custom": None}
            ],
        }
    )

    assert isinstance(approval, AhaAgentBackendApprovalResponseEvent)
    assert isinstance(question, AhaAgentBackendQuestionResponseEvent)
