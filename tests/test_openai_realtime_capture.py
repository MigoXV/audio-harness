from __future__ import annotations

import json
import re
import stat
from collections.abc import Iterator
from importlib.metadata import version
from pathlib import Path

import pytest
from openai.types.realtime import RealtimeServerEvent
from pydantic import TypeAdapter

from scripts.capture_openai_realtime import (
    CaptureError,
    RawCaptureWriter,
    TimelineEntry,
    _parse_static_assignment,
    load_openai_api_key,
    normalize_capture,
    prepare_raw_directory,
)

_SERVER_EVENT_ADAPTER = TypeAdapter(RealtimeServerEvent)
_FIXTURE_ROOT = Path(__file__).parent / "fixtures/openai_realtime"


def test_api_key_loader_uses_environment_before_dotenv(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".env").write_text(
        "OPENAI_API_KEY=dotenv-key-123456789\n", encoding="utf-8"
    )

    loaded = load_openai_api_key(
        repo_root=repo_root,
        environ={"OPENAI_API_KEY": "environment-key-123456789"},
    )

    assert loaded.source == "process environment"
    assert loaded.value == "environment-key-123456789"
    assert loaded.value not in repr(loaded)


def test_api_key_loader_reads_dotenv_without_executing_it(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    marker = tmp_path / "must-not-exist"
    repo_root.mkdir()
    (repo_root / ".env").write_text(
        f"OPENAI_API_KEY=$(touch {marker})\n", encoding="utf-8"
    )

    with pytest.raises(CaptureError, match="未找到安全"):
        load_openai_api_key(repo_root=repo_root, environ={})

    assert not marker.exists()


def test_static_assignment_uses_last_assignment_and_rejects_expansion(
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENAI_API_KEY=first-static-key-123456789\n"
        "OPENAI_API_KEY=${OPENAI_API_KEY_FROM_SHELL}\n",
        encoding="utf-8",
    )

    assert _parse_static_assignment(dotenv) is None


def test_raw_directory_rejects_unignored_repository_path(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(CaptureError, match="未被 Git 忽略"):
        prepare_raw_directory(repo_root / "raw-capture", repo_root=repo_root)

    assert not (repo_root / "raw-capture").exists()


def test_default_raw_directory_rejects_unsafe_tmpdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    raw_directory = repo_root / "unsafe-system-temp"
    repo_root.mkdir()

    def fake_mkdtemp(*, prefix: str) -> str:
        assert prefix == "audio-harness-realtime-capture-"
        raw_directory.mkdir()
        return str(raw_directory)

    monkeypatch.setattr(
        "scripts.capture_openai_realtime.tempfile.mkdtemp",
        fake_mkdtemp,
    )

    with pytest.raises(CaptureError, match="系统临时目录"):
        prepare_raw_directory(None, repo_root=repo_root)

    assert not raw_directory.exists()


def test_raw_writer_uses_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "capture.raw.jsonl"
    with RawCaptureWriter(path) as writer:
        writer.record(
            TimelineEntry(
                direction="client",
                payload={"type": "input_audio_buffer.clear"},
            )
        )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "direction": "client",
        "payload": {"type": "input_audio_buffer.clear"},
    }


def test_normalization_preserves_order_relations_and_error_codes() -> None:
    timeline = [
        TimelineEntry(
            direction="client",
            payload={
                "type": "input_audio_buffer.append",
                "event_id": "event_original",
                "audio": "cHJpdmF0ZSBhdWRpbw==",
                "authorization": "Bearer secret-value-123456789",
            },
        ),
        TimelineEntry(
            direction="server",
            payload={
                "type": "error",
                "event_id": "event_server",
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_value",
                    "message": "The request was invalid.",
                    "param": None,
                    "event_id": "event_original",
                },
                "expires_at": 1234567890,
                "conversation_id": "conv_original",
                "obfuscation": "random-padding",
            },
        ),
    ]

    document = normalize_capture(
        kind="transcription",
        model="gpt-live-transcribe",
        timeline=timeline,
        sdk_version="2.50.0",
    )

    assert [entry["sequence"] for entry in document["timeline"]] == [1, 2]
    first = document["timeline"][0]["payload"]
    second = document["timeline"][1]["payload"]
    assert first["audio"] == "<audio_1>"
    assert first["authorization"] == "<redacted>"
    assert second["error"]["event_id"] == first["event_id"]
    assert second["error"]["code"] == "invalid_value"
    assert second["error"]["message"] == "<text_1>"
    assert second["expires_at"] == 0
    assert second["conversation_id"] == "<conversation_id_1>"
    assert second["obfuscation"] == "<opaque_1>"


@pytest.mark.parametrize("kind", ["transcription", "realtime"])
def test_live_normalized_fixture_is_safe_and_sdk_conformant(kind: str) -> None:
    fixture_path = _FIXTURE_ROOT / f"openai-{kind}.normalized.json"
    document = json.loads(fixture_path.read_text(encoding="utf-8"))
    serialized = json.dumps(document, ensure_ascii=False)

    assert document["capture"]["kind"] == kind
    assert document["capture"]["query"] == (
        {"intent": "transcription"}
        if kind == "transcription"
        else {"model": "gpt-realtime"}
    )
    assert document["source"]["sdk"] == {
        "package": "openai",
        "version": version("openai"),
    }
    assert document["source"]["server_events_validated"] is True
    assert re.search(r"\bsk-[A-Za-z0-9_.-]{16,}\b", serialized) is None
    assert "Bearer " not in serialized
    assert max(map(len, _json_strings(document))) <= 512
    assert [entry["sequence"] for entry in document["timeline"]] == list(
        range(1, len(document["timeline"]) + 1)
    )

    server_events = [
        entry["payload"]
        for entry in document["timeline"]
        if entry["direction"] == "server"
    ]
    for event in server_events:
        _SERVER_EVENT_ADAPTER.validate_python(event)

    event_types = {event["type"] for event in server_events}
    assert "session.created" in event_types
    assert "session.updated" in event_types
    if kind == "transcription":
        assert "input_audio_buffer.committed" in event_types
        assert (
            "conversation.item.input_audio_transcription.completed" in event_types
        )
    else:
        assert "response.done" in event_types
        assert "conversation.created" not in event_types
        assert "conversation.item.created" not in event_types
        assert "conversation.item.added" in event_types
        assert "conversation.item.done" in event_types
        assert "conversation.item.retrieved" in event_types
        assert "conversation.item.deleted" in event_types
        errors = [
            event["error"]
            for event in server_events
            if event["type"] == "error"
        ]
        assert errors
        assert all(error.get("code") for error in errors)
        assert any(
            event["response"]["status"] == "cancelled"
            for event in server_events
            if event["type"] == "response.done"
        )


def _json_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _json_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_strings(child)
