"""记录人工引擎连通性校验失败状态。

Revision ID: 0008_engine_validation_status
Revises: 0007_multi_workflow_agent
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "0008_engine_validation_status"
down_revision = "0007_multi_workflow_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("engine_assets") as batch_op:
        batch_op.drop_constraint(
            "ck_engine_assets_status",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_engine_assets_status",
            "status IN ("
            "'observed_running', 'confirmed_running', "
            "'unavailable', 'inactive'"
            ")",
        )


def downgrade() -> None:
    with op.batch_alter_table("engine_assets") as batch_op:
        batch_op.drop_constraint(
            "ck_engine_assets_status",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_engine_assets_status",
            "status IN ('observed_running', 'confirmed_running', 'inactive')",
        )
