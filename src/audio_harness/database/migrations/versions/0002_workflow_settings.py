"""持久化工作流与容量配置。

Revision ID: 0002_workflow_settings
Revises: 0001_engine_assets
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_workflow_settings"
down_revision = "0001_engine_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    workflow_settings = op.create_table(
        "workflow_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("min_chunk_bytes", sa.Integer(), nullable=False),
        sa.Column("min_segment_seconds", sa.Float(), nullable=False),
        sa.Column("max_segment_seconds", sa.Float(), nullable=False),
        sa.Column("audio_buffer_seconds", sa.Float(), nullable=False),
        sa.Column("speech_pre_roll_seconds", sa.Float(), nullable=False),
        sa.Column("default_language_code", sa.String(length=32), nullable=False),
        sa.Column("audio_queue_capacity", sa.Integer(), nullable=False),
        sa.Column("segment_queue_capacity", sa.Integer(), nullable=False),
        sa.Column("result_queue_capacity", sa.Integer(), nullable=False),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("id = 1", name="ck_workflow_settings_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        workflow_settings,
        [
            {
                "id": 1,
                "min_chunk_bytes": 6400,
                "min_segment_seconds": 0.5,
                "max_segment_seconds": 60.0,
                "audio_buffer_seconds": 120.0,
                "speech_pre_roll_seconds": 0.3,
                "default_language_code": "zh-CN",
                "audio_queue_capacity": 32,
                "segment_queue_capacity": 8,
                "result_queue_capacity": 32,
                "revision": 1,
            }
        ],
    )
    capacity_settings = op.create_table(
        "capacity_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("grpc_max_sessions", sa.Integer(), nullable=False),
        sa.Column("realtime_max_sessions", sa.Integer(), nullable=False),
        sa.Column("max_asr_concurrency", sa.Integer(), nullable=False),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("id = 1", name="ck_capacity_settings_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        capacity_settings,
        [
            {
                "id": 1,
                "grpc_max_sessions": 64,
                "realtime_max_sessions": 64,
                "max_asr_concurrency": 8,
                "revision": 1,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("capacity_settings")
    op.drop_table("workflow_settings")
