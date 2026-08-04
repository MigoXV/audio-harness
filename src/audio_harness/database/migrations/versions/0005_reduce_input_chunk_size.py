"""将输入重分块默认值调整为 640 字节。

Revision ID: 0005_reduce_input_chunk_size
Revises: 0004_remove_asr_vad_kind
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_reduce_input_chunk_size"
down_revision = "0004_remove_asr_vad_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE workflow_settings "
            "SET min_chunk_bytes = 640, "
            "revision = revision + 1, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE min_chunk_bytes = 6400"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE workflow_settings "
            "SET min_chunk_bytes = 6400, "
            "revision = revision + 1, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE min_chunk_bytes = 640"
        )
    )
