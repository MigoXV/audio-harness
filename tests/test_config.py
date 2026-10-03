from pathlib import Path

import pytest
from pydantic import ValidationError

from audio_harness.configs.app import AppConfig
from audio_harness.configs.webrtc import parse_ice_servers_json


def test_load_config_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "sqlite+aiosqlite:///./data/test.db",
    )
    monkeypatch.setenv("VOICE_STORAGE_DIR", "./data/test-voices")
    monkeypatch.setenv("RECEIVED_AUDIO_CACHE_DIR", "./data/test-cache")

    config = AppConfig.from_environment()

    assert config.database.url == "sqlite+aiosqlite:///./data/test.db"
    assert config.voices.storage_dir == Path("data/test-voices")
    assert config.voices.max_upload_bytes == 20 * 1024 * 1024
    assert config.received_audio_cache.storage_dir == Path("data/test-cache")
    assert config.workflow.min_chunk_bytes == 640
    assert config.http.port == 8000
    assert config.limits.max_message_bytes == 4 * 1024 * 1024
    assert config.webrtc.ice_servers == []


def test_listen_ports_allow_automatic_selection() -> None:
    config = AppConfig.model_validate(
        {
            "http": {"port": 0},
        }
    )

    assert config.http.port == 0


def test_rejects_removed_grpc_listener_config() -> None:
    with pytest.raises(ValidationError, match="grpc"):
        AppConfig.model_validate({"grpc": {"port": 50051}})


def test_rejects_odd_pcm_chunk_size() -> None:
    with pytest.raises(ValidationError, match="min_chunk_bytes"):
        AppConfig.model_validate(
            {
                "workflow": {"min_chunk_bytes": 3},
            }
        )


def test_rejects_invalid_received_audio_cache_periods() -> None:
    with pytest.raises(ValidationError, match="整数倍"):
        AppConfig.model_validate(
            {
                "workflow": {
                    "received_audio_cache_block_seconds": 6,
                    "received_audio_cache_file_seconds": 10,
                }
            }
        )


def test_rejects_non_async_sqlite_database() -> None:
    with pytest.raises(ValidationError, match="sqlite\\+aiosqlite"):
        AppConfig.model_validate(
            {
                "database": {"url": "sqlite:///./data/test.db"},
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
