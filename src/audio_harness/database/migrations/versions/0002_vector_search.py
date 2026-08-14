"""新增向量检索运行容量。

Revision ID: 0002_vector_search
Revises: 0001_initial
Create Date: 2026-08-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_vector_search"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("capacity_settings", sa.Column("max_embedding_concurrency", sa.Integer(), nullable=False, server_default="8"))
    op.add_column("capacity_settings", sa.Column("max_qdrant_concurrency", sa.Integer(), nullable=False, server_default="8"))


def downgrade() -> None:
    op.drop_column("capacity_settings", "max_qdrant_concurrency")
    op.drop_column("capacity_settings", "max_embedding_concurrency")
