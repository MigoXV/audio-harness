"""增加 WebRTC 独立会话容量。

Revision ID: 0003_webrtc_capacity
Revises: 0002_workflow_settings
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_webrtc_capacity"
down_revision = "0002_workflow_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "capacity_settings",
        sa.Column(
            "webrtc_max_sessions",
            sa.Integer(),
            nullable=False,
            server_default="64",
        ),
    )


def downgrade() -> None:
    op.drop_column("capacity_settings", "webrtc_max_sessions")
