"""增加第二代控制面引擎凭据与健康探测状态。

Revision ID: 0009_control_plane_v2
Revises: 0008_engine_validation_status
Create Date: 2026-08-04
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0009_control_plane_v2"
down_revision = "0008_engine_validation_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("engine_assets") as batch_op:
        batch_op.add_column(sa.Column("api_key", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("probe_latency_ms", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "consecutive_failures",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column("next_probe_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.drop_constraint("ck_engine_assets_status", type_="check")
        batch_op.create_check_constraint(
            "ck_engine_assets_status",
            "status IN ("
            "'unknown', 'checking', 'healthy', 'unhealthy', 'stale', "
            "'disabled', 'observed_running', 'confirmed_running', "
            "'unavailable', 'inactive'"
            ")",
        )

    op.execute(
        "UPDATE engine_assets SET status = CASE "
        "WHEN enabled = 0 THEN 'disabled' ELSE 'unknown' END"
    )

    # 0007 会为全新数据库建立一个旧 DAG 示例。第二代控制面不继续暴露可编辑
    # 图，因此在本代迁移末尾把该示例改为约束式阶段定义，并清除旧运行修订。
    connection = op.get_bind()
    raw_definition = connection.execute(
        sa.text(
            "SELECT draft_definition FROM workflows "
            "WHERE id = 'default-transcription'"
        )
    ).scalar_one_or_none()
    if raw_definition is not None:
        legacy = (
            json.loads(raw_definition)
            if isinstance(raw_definition, str)
            else raw_definition
        )
        nodes = {node["type"]: node for node in legacy.get("nodes", [])}
        physical = nodes.get("physical_vad", {})
        semantic = nodes.get("semantic_vad")
        asr = nodes.get("asr", {})
        stages = {
            "physical_vad": {
                "engine_id": physical.get("engine_id"),
                "config": physical.get("config", {}),
            },
            "semantic_vad": (
                {
                    "engine_id": semantic.get("engine_id"),
                    "config": semantic.get("config", {}),
                    "failure_policy": semantic.get(
                        "failure_policy", "fail_open"
                    ),
                }
                if semantic is not None
                else None
            ),
            "asr": {
                "engine_id": asr.get("engine_id"),
                "config": asr.get("config", {}),
            },
            "responses_agent": None,
            "duplex_tts": None,
        }
        definition = json.dumps(
            {"schema_version": 2, "stages": stages},
            ensure_ascii=False,
        )
        connection.execute(
            sa.text(
                "UPDATE workflow_activations SET active_revision_id = NULL "
                "WHERE workflow_id = 'default-transcription'"
            )
        )
        connection.execute(
            sa.text(
                "DELETE FROM workflow_revisions "
                "WHERE workflow_id = 'default-transcription'"
            )
        )
        connection.execute(
            sa.text(
                "UPDATE workflows SET draft_definition = :definition, "
                "description = '第二代控制面默认转写工作流' "
                "WHERE id = 'default-transcription'"
            ),
            {"definition": definition},
        )
        connection.execute(
            sa.text(
                "DELETE FROM model_routes "
                "WHERE model = 'gpt-4o-transcribe'"
            )
        )
        connection.execute(
            sa.text(
                "INSERT OR IGNORE INTO model_routes "
                "(model, workflow_id, visibility, created_at, updated_at) "
                "SELECT slug, id, 'public', created_at, updated_at "
                "FROM workflows WHERE id = 'default-transcription'"
            )
        )


def downgrade() -> None:
    op.execute(
        "UPDATE engine_assets SET status = CASE "
        "WHEN enabled = 0 THEN 'inactive' ELSE 'observed_running' END"
    )
    with op.batch_alter_table("engine_assets") as batch_op:
        batch_op.drop_constraint("ck_engine_assets_status", type_="check")
        batch_op.create_check_constraint(
            "ck_engine_assets_status",
            "status IN ("
            "'observed_running', 'confirmed_running', "
            "'unavailable', 'inactive'"
            ")",
        )
        batch_op.drop_column("next_probe_at")
        batch_op.drop_column("consecutive_failures")
        batch_op.drop_column("probe_latency_ms")
        batch_op.drop_column("last_success_at")
        batch_op.drop_column("api_key")
