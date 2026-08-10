"""建立类型化语音工作流初始数据库。

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-09
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

from audio_harness.workflows.models import default_audio_workflow

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "engine_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("api", sa.String(64), nullable=False),
        sa.Column("protocol", sa.String(32), nullable=False),
        sa.Column("target", sa.String(500), nullable=False),
        sa.Column("connect_timeout_seconds", sa.Float(), nullable=False),
        sa.Column("request_timeout_seconds", sa.Float(), nullable=True),
        sa.Column("credential_env", sa.String(200), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("image", sa.String(500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status_basis", sa.Text(), nullable=True),
        sa.Column("status_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_latency_ms", sa.Float(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(500), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_engine_assets_enabled", "engine_assets", ["enabled"])
    op.create_index("ix_engine_assets_slug", "engine_assets", ["slug"], unique=True)

    op.create_table(
        "audio_workflows",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audio_workflows_slug", "audio_workflows", ["slug"], unique=True)
    op.create_table(
        "audio_workflow_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "audio_workflow_id",
            sa.String(100),
            sa.ForeignKey("audio_workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("compiled_plan", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "audio_workflow_id",
            "revision",
            name="uq_audio_workflow_revision_number",
        ),
    )
    op.create_index(
        "ix_audio_workflow_revisions_audio_workflow_id",
        "audio_workflow_revisions",
        ["audio_workflow_id"],
    )
    op.create_table(
        "audio_workflow_activations",
        sa.Column(
            "audio_workflow_id",
            sa.String(100),
            sa.ForeignKey("audio_workflows.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "active_revision_id",
            sa.Integer(),
            sa.ForeignKey("audio_workflow_revisions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "workflow_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("min_chunk_bytes", sa.Integer(), nullable=False),
        sa.Column("default_language_code", sa.String(32), nullable=False),
        sa.Column("audio_queue_capacity", sa.Integer(), nullable=False),
        sa.Column("segment_queue_capacity", sa.Integer(), nullable=False),
        sa.Column("result_queue_capacity", sa.Integer(), nullable=False),
        sa.Column("grpc_audio_workflow_id", sa.String(100), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "capacity_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("grpc_max_sessions", sa.Integer(), nullable=False),
        sa.Column("realtime_max_sessions", sa.Integer(), nullable=False),
        sa.Column("webrtc_max_sessions", sa.Integer(), nullable=False),
        sa.Column("max_asr_concurrency", sa.Integer(), nullable=False),
        sa.Column("max_responses_concurrency", sa.Integer(), nullable=False),
        sa.Column("max_tts_concurrency", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "voice_agent_profiles",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_definition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_voice_agent_profiles_slug", "voice_agent_profiles", ["slug"], unique=True)
    op.create_table(
        "voice_agent_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "voice_agent_id",
            sa.String(100),
            sa.ForeignKey("voice_agent_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("compiled_profile", sa.JSON(), nullable=False),
        sa.Column(
            "audio_workflow_revision_id",
            sa.Integer(),
            sa.ForeignKey("audio_workflow_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "voice_agent_id",
            "revision",
            name="uq_voice_agent_revision_number",
        ),
    )
    op.create_index(
        "ix_voice_agent_revisions_voice_agent_id",
        "voice_agent_revisions",
        ["voice_agent_id"],
    )
    op.create_table(
        "voice_agent_activations",
        sa.Column(
            "voice_agent_id",
            sa.String(100),
            sa.ForeignKey("voice_agent_profiles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "active_revision_id",
            sa.Integer(),
            sa.ForeignKey("voice_agent_revisions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "realtime_routes",
        sa.Column("model", sa.String(100), primary_key=True),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(100), nullable=False),
        sa.Column("visibility", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_realtime_routes_target_id", "realtime_routes", ["target_id"])

    op.create_table(
        "voice_assets",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("wav_path", sa.String(500), nullable=False, unique=True),
        sa.Column("ref_text", sa.Text(), nullable=True),
        sa.Column("sample_rate", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    now = datetime.now(timezone.utc)
    default_id = "default-audio"
    op.bulk_insert(
        sa.table(
            "audio_workflows",
            sa.column("id", sa.String),
            sa.column("slug", sa.String),
            sa.column("name", sa.String),
            sa.column("description", sa.Text),
            sa.column("draft_revision", sa.Integer),
            sa.column("draft_document", sa.JSON),
            sa.column("created_at", sa.DateTime),
            sa.column("updated_at", sa.DateTime),
        ),
        [
            {
                "id": default_id,
                "slug": default_id,
                "name": "默认语音工作流",
                "description": "绑定 VAD 与 ASR 引擎后发布",
                "draft_revision": 1,
                "draft_document": default_audio_workflow().model_dump(mode="json"),
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    op.execute(
        sa.text(
            "INSERT INTO audio_workflow_activations "
            "(audio_workflow_id, active_revision_id, updated_at) "
            "VALUES ('default-audio', NULL, CURRENT_TIMESTAMP)"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO workflow_settings "
            "(id, min_chunk_bytes, default_language_code, audio_queue_capacity, "
            "segment_queue_capacity, result_queue_capacity, grpc_audio_workflow_id, "
            "revision, updated_at) VALUES "
            "(1, 640, 'zh-CN', 32, 8, 32, 'default-audio', 1, CURRENT_TIMESTAMP)"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO capacity_settings "
            "(id, grpc_max_sessions, realtime_max_sessions, webrtc_max_sessions, "
            "max_asr_concurrency, max_responses_concurrency, max_tts_concurrency, "
            "revision, updated_at) VALUES (1, 64, 64, 64, 8, 8, 1, 1, CURRENT_TIMESTAMP)"
        )
    )


def downgrade() -> None:
    op.drop_table("voice_assets")
    op.drop_index("ix_realtime_routes_target_id", table_name="realtime_routes")
    op.drop_table("realtime_routes")
    op.drop_table("voice_agent_activations")
    op.drop_index("ix_voice_agent_revisions_voice_agent_id", table_name="voice_agent_revisions")
    op.drop_table("voice_agent_revisions")
    op.drop_index("ix_voice_agent_profiles_slug", table_name="voice_agent_profiles")
    op.drop_table("voice_agent_profiles")
    op.drop_table("capacity_settings")
    op.drop_table("workflow_settings")
    op.drop_table("audio_workflow_activations")
    op.drop_index(
        "ix_audio_workflow_revisions_audio_workflow_id",
        table_name="audio_workflow_revisions",
    )
    op.drop_table("audio_workflow_revisions")
    op.drop_index("ix_audio_workflows_slug", table_name="audio_workflows")
    op.drop_table("audio_workflows")
    op.drop_index("ix_engine_assets_slug", table_name="engine_assets")
    op.drop_index("ix_engine_assets_enabled", table_name="engine_assets")
    op.drop_table("engine_assets")
