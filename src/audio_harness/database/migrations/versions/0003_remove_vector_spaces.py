"""移除本地向量空间注册表。

Revision ID: 0003_remove_vector_spaces
Revises: 0002_vector_search
Create Date: 2026-08-14
"""

from alembic import op
from sqlalchemy import inspect

revision = "0003_remove_vector_spaces"
down_revision = "0002_vector_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if inspect(op.get_bind()).has_table("vector_spaces"):
        op.drop_index("ix_vector_spaces_slug", table_name="vector_spaces")
        op.drop_table("vector_spaces")


def downgrade() -> None:
    pass
