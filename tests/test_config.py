from pathlib import Path

import pytest
from pydantic import ValidationError

from audio_harness.config import AppConfig, parse_ice_servers_json


def test_load_config_expands_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "sqlite+aiosqlite:///./data/test.db",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
database:
  url: ${TEST_DATABASE_URL}
""",
        encoding="utf-8",
    )

    config = AppConfig.load(config_path)

    assert config.database.url == "sqlite+aiosqlite:///./data/test.db"
    assert config.workflow.min_chunk_bytes == 6400
    assert config.workflow.audio_buffer_seconds == 120
    assert config.workflow.speech_pre_roll_seconds == 0.3
    assert config.grpc.port == 50051
    assert config.http.port == 8000
    assert config.limits.max_message_bytes == 4 * 1024 * 1024
    assert config.webrtc.ice_servers == []


def test_rejects_odd_pcm_chunk_size() -> None:
    with pytest.raises(ValidationError, match="min_chunk_bytes"):
        AppConfig.model_validate(
            {
                "workflow": {"min_chunk_bytes": 3},
            }
        )


def test_rejects_non_async_sqlite_database() -> None:
    with pytest.raises(ValidationError, match="sqlite\\+aiosqlite"):
        AppConfig.model_validate(
            {
                "database": {"url": "sqlite:///./data/test.db"},
            }
        )


def test_rejects_negative_speech_pre_roll() -> None:
    with pytest.raises(ValidationError, match="speech_pre_roll_seconds"):
        AppConfig.model_validate(
            {
                "workflow": {"speech_pre_roll_seconds": -0.1},
            }
        )


def test_rejects_audio_buffer_shorter_than_segment_window() -> None:
    with pytest.raises(ValidationError, match="audio_buffer_seconds"):
        AppConfig.model_validate(
            {
                "workflow": {
                    "audio_buffer_seconds": 60,
                    "max_segment_seconds": 60,
                    "speech_pre_roll_seconds": 0.3,
                },
            }
        )


def test_parses_webrtc_ice_servers_with_secret_credentials() -> None:
    servers = parse_ice_servers_json(
        '[{"urls":["stun:ice.example:3478",'
        '"turns:ice.example:5349"],'
        '"username":"demo","credential":"secret"}]'
    )

    assert servers[0].urls == [
        "stun:ice.example:3478",
        "turns:ice.example:5349",
    ]
    assert servers[0].credential is not None
    assert servers[0].credential.get_secret_value() == "secret"


@pytest.mark.parametrize(
    "value",
    ['{"urls":"stun:ice.example"}', '[{"urls":"https://ice.example"}]'],
)
def test_rejects_invalid_webrtc_ice_servers(value: str) -> None:
    with pytest.raises((ValueError, ValidationError), match="ICE|数组"):
        parse_ice_servers_json(value)
