"""移除 ASR/VAD 组合引擎类型。

Revision ID: 0004_remove_asr_vad_kind
Revises: 0003_webrtc_capacity
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_remove_asr_vad_kind"
down_revision = "0003_webrtc_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE engine_assets SET kind = 'asr' "
            "WHERE kind = 'asr_vad'"
        )
    )
    with op.batch_alter_table("engine_assets") as batch_op:
        batch_op.drop_constraint(
            "ck_engine_assets_kind",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_engine_assets_kind",
            "kind IN ('asr', 'vad')",
        )


def downgrade() -> None:
    with op.batch_alter_table("engine_assets") as batch_op:
        batch_op.drop_constraint(
            "ck_engine_assets_kind",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_engine_assets_kind",
            "kind IN ('asr', 'vad', 'asr_vad')",
        )
