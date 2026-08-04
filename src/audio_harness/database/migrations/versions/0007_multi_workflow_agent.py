"""增加多工作流、模型路由与 Agent 引擎能力。

Revision ID: 0007_multi_workflow_agent
Revises: 0006_pipeline_dag
Create Date: 2026-08-03
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0007_multi_workflow_agent"
down_revision = "0006_pipeline_dag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("engine_assets") as batch_op:
        batch_op.add_column(
            sa.Column("credential_env", sa.String(length=200), nullable=True)
        )
        batch_op.drop_constraint("ck_engine_assets_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_engine_assets_kind",
            "kind IN ('asr', 'vad', 'llm', 'tts')",
        )
        batch_op.drop_constraint("ck_engine_assets_api", type_="check")
        batch_op.create_check_constraint(
            "ck_engine_assets_api",
            "api IN ('ux_speech', 'ux_vad', 'responses', "
            "'qwen_tts_duplex')",
        )
        batch_op.drop_constraint("ck_engine_assets_protocol", type_="check")
        batch_op.create_check_constraint(
            "ck_engine_assets_protocol",
            "protocol IN ('grpc', 'https')",
        )

    with op.batch_alter_table("capacity_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "max_responses_concurrency",
                sa.Integer(),
                nullable=False,
                server_default="8",
            )
        )
        batch_op.add_column(
            sa.Column(
                "max_tts_concurrency",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )

    op.create_table(
        "workflows",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("slug", sa.String(length=100), nullable=False, unique=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_definition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('transcription', 'voice_agent')",
            name="ck_workflows_kind",
        ),
    )
    op.create_index("ix_workflows_slug", "workflows", ["slug"], unique=True)

    op.create_table(
        "workflow_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "workflow_id",
            sa.String(length=100),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("compiled_plan", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "workflow_id",
            "revision",
            name="uq_workflow_revision_number",
        ),
    )
    op.create_index(
        "ix_workflow_revisions_workflow_id",
        "workflow_revisions",
        ["workflow_id"],
    )

    op.create_table(
        "workflow_activations",
        sa.Column(
            "workflow_id",
            sa.String(length=100),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "active_revision_id",
            sa.Integer(),
            sa.ForeignKey("workflow_revisions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "model_routes",
        sa.Column("model", sa.String(length=100), primary_key=True),
        sa.Column(
            "workflow_id",
            sa.String(length=100),
            sa.ForeignKey("workflows.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("visibility", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "visibility IN ('public', 'internal')",
            name="ck_model_routes_visibility",
        ),
    )
    op.create_index(
        "ix_model_routes_workflow_id",
        "model_routes",
        ["workflow_id"],
    )

    op.create_table(
        "voice_assets",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("wav_path", sa.String(length=500), nullable=False, unique=True),
        sa.Column("ref_text", sa.Text(), nullable=True),
        sa.Column("sample_rate", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    connection = op.get_bind()
    now = datetime.now(timezone.utc)
    working = connection.execute(
        sa.text(
            "SELECT revision, definition, updated_at "
            "FROM pipeline_working_copy WHERE id = 1"
        )
    ).mappings().one()
    connection.execute(
        sa.text(
            "INSERT INTO workflows "
            "(id, slug, name, kind, description, draft_revision, "
            "draft_definition, created_at, updated_at) "
            "VALUES (:id, :slug, :name, :kind, :description, :revision, "
            ":definition, :created_at, :updated_at)"
        ),
        {
            "id": "default-transcription",
            "slug": "default-transcription",
            "name": "默认转写工作流",
            "kind": "transcription",
            "description": "由升级迁移生成的默认转写工作流",
            "revision": working["revision"],
            "definition": working["definition"],
            "created_at": now,
            "updated_at": working["updated_at"],
        },
    )

    revision_ids: dict[int, int] = {}
    rows = connection.execute(
        sa.text(
            "SELECT revision, definition, compiled_plan, created_at, "
            "activated_at FROM pipeline_revisions ORDER BY revision"
        )
    ).mappings()
    for row in rows:
        result = connection.execute(
            sa.text(
                "INSERT INTO workflow_revisions "
                "(workflow_id, revision, definition, compiled_plan, "
                "created_at, activated_at) VALUES "
                "(:workflow_id, :revision, :definition, :compiled_plan, "
                ":created_at, :activated_at)"
            ),
            {
                "workflow_id": "default-transcription",
                **dict(row),
            },
        )
        revision_ids[int(row["revision"])] = int(result.lastrowid)

    old_active = connection.execute(
        sa.text("SELECT active_revision FROM pipeline_activation WHERE id = 1")
    ).scalar_one()
    connection.execute(
        sa.text(
            "INSERT INTO workflow_activations "
            "(workflow_id, active_revision_id, updated_at) "
            "VALUES (:workflow_id, :active_revision_id, :updated_at)"
        ),
        {
            "workflow_id": "default-transcription",
            "active_revision_id": (
                revision_ids.get(int(old_active)) if old_active is not None else None
            ),
            "updated_at": now,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO model_routes "
            "(model, workflow_id, visibility, created_at, updated_at) VALUES "
            "(:public_model, :workflow_id, 'public', :now, :now), "
            "(:grpc_model, :workflow_id, 'internal', :now, :now)"
        ),
        {
            "public_model": "gpt-4o-transcribe",
            "grpc_model": "grpc-default",
            "workflow_id": "default-transcription",
            "now": now,
        },
    )


def downgrade() -> None:
    op.drop_table("voice_assets")
    op.drop_index("ix_model_routes_workflow_id", table_name="model_routes")
    op.drop_table("model_routes")
    op.drop_table("workflow_activations")
    op.drop_index(
        "ix_workflow_revisions_workflow_id",
        table_name="workflow_revisions",
    )
    op.drop_table("workflow_revisions")
    op.drop_index("ix_workflows_slug", table_name="workflows")
    op.drop_table("workflows")

    with op.batch_alter_table("capacity_settings") as batch_op:
        batch_op.drop_column("max_tts_concurrency")
        batch_op.drop_column("max_responses_concurrency")

    with op.batch_alter_table("engine_assets") as batch_op:
        batch_op.drop_constraint("ck_engine_assets_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_engine_assets_kind",
            "kind IN ('asr', 'vad')",
        )
        batch_op.drop_constraint("ck_engine_assets_api", type_="check")
        batch_op.create_check_constraint(
            "ck_engine_assets_api",
            "api IN ('ux_speech', 'ux_vad')",
        )
        batch_op.drop_constraint("ck_engine_assets_protocol", type_="check")
        batch_op.create_check_constraint(
            "ck_engine_assets_protocol",
            "protocol IN ('grpc')",
        )
        batch_op.drop_column("credential_env")
