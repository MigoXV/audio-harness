from pathlib import Path

import pytest
from pydantic import ValidationError

from audio_harness.config import AppConfig


def test_load_config_expands_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_ASR_TARGET", "127.0.0.1:50008")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
engines:
  asr:
    target: ${TEST_ASR_TARGET}
  vad:
    target: 127.0.0.1:53001
""",
        encoding="utf-8",
    )

    config = AppConfig.load(config_path)

    assert config.engines.asr.target == "127.0.0.1:50008"
    assert config.workflow.min_chunk_bytes == 6400
    assert config.serving.port == 50051


def test_rejects_odd_pcm_chunk_size() -> None:
    with pytest.raises(ValidationError, match="min_chunk_bytes"):
        AppConfig.model_validate(
            {
                "engines": {
                    "asr": {"target": "asr:1"},
                    "vad": {"target": "vad:2"},
                },
                "workflow": {"min_chunk_bytes": 3},
            }
        )


def test_rejects_unexpanded_engine_environment() -> None:
    with pytest.raises(ValidationError, match="未展开"):
        AppConfig.model_validate(
            {
                "engines": {
                    "asr": {"target": "${MISSING_ASR}"},
                    "vad": {"target": "vad:2"},
                }
            }
        )
