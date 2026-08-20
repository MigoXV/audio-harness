"""新增 Agent Backend 控制面资源。

Revision ID: 0006_agent_backends
Revises: 0005_received_audio_cache
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_agent_backends"
down_revision = "0005_received_audio_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_backend_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("protocol", sa.String(64), nullable=False),
        sa.Column("target", sa.String(2048), nullable=False),
        sa.Column("connect_timeout_seconds", sa.Float(), nullable=False),
        sa.Column("request_timeout_seconds", sa.Float(), nullable=False),
        sa.Column("headers_env", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status_basis", sa.Text(), nullable=True),
        sa.Column("status_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_latency_ms", sa.Float(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_agent_backend_assets_enabled",
        "agent_backend_assets",
        ["enabled"],
    )
    op.create_index(
        "ix_agent_backend_assets_slug",
        "agent_backend_assets",
        ["slug"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_backend_assets_slug",
        table_name="agent_backend_assets",
    )
    op.drop_index(
        "ix_agent_backend_assets_enabled",
        table_name="agent_backend_assets",
    )
    op.drop_table("agent_backend_assets")
