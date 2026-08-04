from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from audio_harness.database.session import require_control_plane_v2_database


def migration_config(database_path: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(
            Path(__file__).parents[1]
            / "src"
            / "audio_harness"
            / "database"
            / "migrations"
        ),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def test_legacy_bindings_migrate_to_dag_and_downgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    config = migration_config(database_path)
    command.upgrade(config, "0005_reduce_input_chunk_size")

    asr_id = "10000000-0000-4000-8000-000000000001"
    vad_id = "20000000-0000-4000-8000-000000000001"
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            "INSERT INTO engine_bindings "
            "(role, engine_id, generation, updated_at) "
            "VALUES (?, ?, 1, CURRENT_TIMESTAMP)",
            [("asr", asr_id), ("vad", vad_id)],
        )
        connection.execute(
            "UPDATE workflow_settings SET "
            "min_segment_seconds = 0.8, "
            "max_segment_seconds = 42, "
            "audio_buffer_seconds = 48, "
            "speech_pre_roll_seconds = 0.6 "
            "WHERE id = 1"
        )

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        raw_definition = connection.execute(
            "SELECT definition FROM pipeline_working_copy WHERE id = 1"
        ).fetchone()[0]
        definition = json.loads(raw_definition)
        nodes = {node["type"]: node for node in definition["nodes"]}
        assert nodes["asr"]["engine_id"] == asr_id
        assert nodes["physical_vad"]["engine_id"] == vad_id
        assert nodes["physical_vad"]["config"] == {
            "min_segment_seconds": 0.8,
            "max_segment_seconds": 42.0,
            "audio_buffer_seconds": 48.0,
            "speech_pre_roll_seconds": 0.6,
        }
        assert connection.execute(
            "SELECT active_revision FROM pipeline_activation WHERE id = 1"
        ).fetchone()[0] == 1
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(workflow_settings)"
            )
        }
        assert "min_segment_seconds" not in columns
        assert connection.execute(
            "SELECT COUNT(*) FROM pipeline_revisions"
        ).fetchone()[0] == 1
        workflow_definition = json.loads(
            connection.execute(
                "SELECT draft_definition FROM workflows "
                "WHERE id = 'default-transcription'"
            ).fetchone()[0]
        )
        assert workflow_definition["schema_version"] == 2
        assert connection.execute(
            "SELECT active_revision_id FROM workflow_activations "
            "WHERE workflow_id = 'default-transcription'"
        ).fetchone()[0] is None

    command.downgrade(config, "0005_reduce_input_chunk_size")

    with sqlite3.connect(database_path) as connection:
        assert dict(
            connection.execute(
                "SELECT role, engine_id FROM engine_bindings"
            ).fetchall()
        ) == {"asr": asr_id, "vad": vad_id}
        restored = connection.execute(
            "SELECT min_segment_seconds, max_segment_seconds, "
            "audio_buffer_seconds, speech_pre_roll_seconds "
            "FROM workflow_settings WHERE id = 1"
        ).fetchone()
        assert restored == (0.8, 42.0, 48.0, 0.6)


def test_service_requires_manual_reset_for_pre_v2_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "old-control-plane.db"
    config = migration_config(database_path)
    command.upgrade(config, "0008_engine_validation_status")
    database_url = f"sqlite+aiosqlite:///{database_path}"

    try:
        require_control_plane_v2_database(database_url)
    except RuntimeError as exc:
        assert "不支持原地升级" in str(exc)
        assert str(database_path) in str(exc)
    else:
        raise AssertionError("旧数据库必须要求人工清理")

    command.upgrade(config, "head")
    require_control_plane_v2_database(database_url)
