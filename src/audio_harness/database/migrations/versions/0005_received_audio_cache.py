"""新增接收音频缓存运行设置。

Revision ID: 0005_received_audio_cache
Revises: 0004_remove_grpc_server
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_received_audio_cache"
down_revision = "0004_remove_grpc_server"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("workflow_settings"):
        return
    op.add_column(
        "workflow_settings",
        sa.Column(
            "received_audio_cache_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "workflow_settings",
        sa.Column(
            "received_audio_cache_block_seconds",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )
    op.add_column(
        "workflow_settings",
        sa.Column(
            "received_audio_cache_file_seconds",
            sa.Integer(),
            nullable=False,
            server_default="300",
        ),
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("workflow_settings"):
        return
    op.drop_column("workflow_settings", "received_audio_cache_file_seconds")
    op.drop_column("workflow_settings", "received_audio_cache_block_seconds")
    op.drop_column("workflow_settings", "received_audio_cache_enabled")
