"""创建 ASR/VAD 引擎资产与角色绑定。

Revision ID: 0001_engine_assets
Revises:
Create Date: 2026-07-29
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0001_engine_assets"
down_revision = None
branch_labels = None
depends_on = None


def _observed_at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def upgrade() -> None:
    engine_assets = op.create_table(
        "engine_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("api", sa.String(length=32), nullable=False),
        sa.Column("protocol", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=500), nullable=False),
        sa.Column(
            "connect_timeout_seconds",
            sa.Float(),
            nullable=False,
            server_default="10",
        ),
        sa.Column("request_timeout_seconds", sa.Float(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("image", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status_basis", sa.Text(), nullable=True),
        sa.Column("status_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=500), nullable=True),
        sa.Column(
            "details",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "kind IN ('asr', 'vad', 'asr_vad')",
            name="ck_engine_assets_kind",
        ),
        sa.CheckConstraint(
            "api IN ('ux_speech', 'ux_vad')",
            name="ck_engine_assets_api",
        ),
        sa.CheckConstraint(
            "protocol IN ('grpc')",
            name="ck_engine_assets_protocol",
        ),
        sa.CheckConstraint(
            "status IN ('observed_running', 'confirmed_running', 'inactive')",
            name="ck_engine_assets_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index(
        "ix_engine_assets_enabled",
        "engine_assets",
        ["enabled"],
        unique=False,
    )
    op.create_index(
        "ix_engine_assets_slug",
        "engine_assets",
        ["slug"],
        unique=True,
    )

    op.create_table(
        "engine_bindings",
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("engine_id", sa.String(length=36), nullable=False),
        sa.Column(
            "generation",
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
        sa.CheckConstraint(
            "role IN ('asr', 'vad', 'semantic_vad')",
            name="ck_engine_bindings_role",
        ),
        sa.ForeignKeyConstraint(
            ["engine_id"],
            ["engine_assets.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("role"),
    )
    op.create_index(
        "ix_engine_bindings_engine_id",
        "engine_bindings",
        ["engine_id"],
        unique=False,
    )

    source = "engines/README.md"
    audit = "2026-07-07 审计时运行；未经后续记录确认，不能视为当前在线"
    confirmed = "2026-07-21 确认在用"
    inactive = "2026-07-29 只读 Docker 查询未发现运行实例"
    op.bulk_insert(
        engine_assets,
        [
            {
                "id": "10000000-0000-4000-8000-000000000001",
                "slug": "qwen-asr-1-7b",
                "name": "Qwen ASR 1.7B",
                "kind": "asr",
                "api": "ux_speech",
                "protocol": "grpc",
                "target": "192.168.0.222:50011",
                "connect_timeout_seconds": 10.0,
                "request_timeout_seconds": 120.0,
                "enabled": True,
                "status": "observed_running",
                "image": "qwen-asr:0.3.1a3",
                "description": "通用 ASR",
                "status_basis": audit,
                "status_observed_at": _observed_at(2026, 7, 7),
                "source": source,
                "details": {"scene": "通用"},
                "revision": 1,
            },
            {
                "id": "10000000-0000-4000-8000-000000000002",
                "slug": "qwen-asr-atc",
                "name": "Qwen ASR ATC",
                "kind": "asr",
                "api": "ux_speech",
                "protocol": "grpc",
                "target": "192.168.0.222:50012",
                "connect_timeout_seconds": 10.0,
                "request_timeout_seconds": 120.0,
                "enabled": True,
                "status": "observed_running",
                "image": "qwen-asr:0.3.2a1-cu128-amd64",
                "description": "ATC 场景 ASR",
                "status_basis": audit,
                "status_observed_at": _observed_at(2026, 7, 7),
                "source": source,
                "details": {"scene": "ATC"},
                "revision": 1,
            },
            {
                "id": "10000000-0000-4000-8000-000000000003",
                "slug": "gz03-asr-vad-sidecar",
                "name": "GZ03 ASR/VAD sidecar",
                "kind": "asr_vad",
                "api": "ux_speech",
                "protocol": "grpc",
                "target": "192.168.0.222:50024",
                "connect_timeout_seconds": 10.0,
                "request_timeout_seconds": 120.0,
                "enabled": True,
                "status": "observed_running",
                "image": "asr-vad:0.13.2a1-amd64-gz03",
                "description": "GZ03 项目组合 ASR/VAD sidecar；对外暴露 UxSpeech",
                "status_basis": audit,
                "status_observed_at": _observed_at(2026, 7, 7),
                "source": source,
                "details": {"scene": "GZ03"},
                "revision": 1,
            },
            {
                "id": "10000000-0000-4000-8000-000000000004",
                "slug": "asr-vad-hyxt",
                "name": "ASR/VAD HYXT",
                "kind": "asr_vad",
                "api": "ux_speech",
                "protocol": "grpc",
                "target": "192.168.0.213:50003",
                "connect_timeout_seconds": 10.0,
                "request_timeout_seconds": 120.0,
                "enabled": True,
                "status": "observed_running",
                "image": "asr-vad:0.12.3a2-amd64",
                "description": "HYXT 场景组合 ASR/VAD；对外暴露 UxSpeech",
                "status_basis": audit,
                "status_observed_at": _observed_at(2026, 7, 7),
                "source": source,
                "details": {"scene": "HYXT"},
                "revision": 1,
            },
            {
                "id": "10000000-0000-4000-8000-000000000005",
                "slug": "asr-vad-turn",
                "name": "ASR/VAD Turn",
                "kind": "asr_vad",
                "api": "ux_speech",
                "protocol": "grpc",
                "target": "192.168.0.213:50055",
                "connect_timeout_seconds": 10.0,
                "request_timeout_seconds": 120.0,
                "enabled": True,
                "status": "observed_running",
                "image": "asr-vad:turna1",
                "description": "Turn 版本组合 ASR/VAD；对外暴露 UxSpeech",
                "status_basis": audit,
                "status_observed_at": _observed_at(2026, 7, 7),
                "source": source,
                "details": {"scene": "Turn"},
                "revision": 1,
            },
            {
                "id": "20000000-0000-4000-8000-000000000001",
                "slug": "fireredvad",
                "name": "FireRedVAD",
                "kind": "vad",
                "api": "ux_vad",
                "protocol": "grpc",
                "target": "192.168.0.222:50021",
                "connect_timeout_seconds": 10.0,
                "request_timeout_seconds": None,
                "enabled": True,
                "status": "observed_running",
                "image": "fireredvad:0.7.0a1-cu128-amd64",
                "description": "通用 VAD 实例",
                "status_basis": audit,
                "status_observed_at": _observed_at(2026, 7, 7),
                "source": source,
                "details": {"scene": "通用"},
                "revision": 1,
            },
            {
                "id": "20000000-0000-4000-8000-000000000002",
                "slug": "fireredvad-500ms",
                "name": "FireRedVAD 500 ms",
                "kind": "vad",
                "api": "ux_vad",
                "protocol": "grpc",
                "target": "192.168.0.222:50022",
                "connect_timeout_seconds": 10.0,
                "request_timeout_seconds": None,
                "enabled": True,
                "status": "confirmed_running",
                "image": "fireredvad:0.7.0a1-cu128-amd64",
                "description": "HierVAD 可选 refine VAD",
                "status_basis": confirmed,
                "status_observed_at": _observed_at(2026, 7, 21),
                "source": source,
                "details": {"scene": "HierVAD refine", "silence_ms": 500},
                "revision": 1,
            },
            {
                "id": "20000000-0000-4000-8000-000000000003",
                "slug": "fireredvad-3s",
                "name": "FireRedVAD 3 秒",
                "kind": "vad",
                "api": "ux_vad",
                "protocol": "grpc",
                "target": "192.168.0.222:50023",
                "connect_timeout_seconds": 10.0,
                "request_timeout_seconds": None,
                "enabled": True,
                "status": "confirmed_running",
                "image": "fireredvad:0.7.0a1-cu128-amd64",
                "description": "HierVAD coarse VAD；32 路已验证",
                "status_basis": confirmed,
                "status_observed_at": _observed_at(2026, 7, 21),
                "source": source,
                "details": {"scene": "HierVAD coarse", "silence_ms": 3000},
                "revision": 1,
            },
            {
                "id": "20000000-0000-4000-8000-000000000004",
                "slug": "fireredvad-migo-voice",
                "name": "FireRedVAD Migo Voice",
                "kind": "vad",
                "api": "ux_vad",
                "protocol": "grpc",
                "target": "192.168.0.222:50030",
                "connect_timeout_seconds": 10.0,
                "request_timeout_seconds": None,
                "enabled": False,
                "status": "inactive",
                "image": "fireredvad:0.4.0-cu128-amd64",
                "description": "Migo Voice 场景历史实例",
                "status_basis": inactive,
                "status_observed_at": _observed_at(2026, 7, 29),
                "source": source,
                "details": {"scene": "Migo Voice"},
                "revision": 1,
            },
            {
                "id": "20000000-0000-4000-8000-000000000005",
                "slug": "ali-fsmn-vad",
                "name": "Ali FSMN VAD",
                "kind": "vad",
                "api": "ux_vad",
                "protocol": "grpc",
                "target": "192.168.0.222:53002",
                "connect_timeout_seconds": 10.0,
                "request_timeout_seconds": None,
                "enabled": False,
                "status": "inactive",
                "image": "ali-fsmn:0.4.0-cu124-amd64",
                "description": "GZ03 历史 VAD 实例",
                "status_basis": inactive,
                "status_observed_at": _observed_at(2026, 7, 29),
                "source": source,
                "details": {"scene": "GZ03"},
                "revision": 1,
            },
            {
                "id": "20000000-0000-4000-8000-000000000006",
                "slug": "fireredvad-gz03-02",
                "name": "FireRedVAD GZ03-02",
                "kind": "vad",
                "api": "ux_vad",
                "protocol": "grpc",
                "target": "192.168.0.222:53003",
                "connect_timeout_seconds": 10.0,
                "request_timeout_seconds": None,
                "enabled": False,
                "status": "inactive",
                "image": "fireredvad:0.3.0post1-cu128-amd64",
                "description": "GZ03 历史 FireRedVAD 实例",
                "status_basis": inactive,
                "status_observed_at": _observed_at(2026, 7, 29),
                "source": source,
                "details": {"scene": "GZ03-02"},
                "revision": 1,
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_engine_bindings_engine_id", table_name="engine_bindings")
    op.drop_table("engine_bindings")
    op.drop_index("ix_engine_assets_slug", table_name="engine_assets")
    op.drop_index("ix_engine_assets_enabled", table_name="engine_assets")
    op.drop_table("engine_assets")
