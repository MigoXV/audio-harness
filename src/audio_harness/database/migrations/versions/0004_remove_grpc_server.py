"""移除对外 gRPC 服务端配置。

Revision ID: 0004_remove_grpc_server
Revises: 0003_remove_vector_spaces
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_remove_grpc_server"
down_revision = "0003_remove_vector_spaces"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {
        column["name"]
        for column in inspector.get_columns(table_name)
    }


def upgrade() -> None:
    if "grpc_audio_workflow_id" in _column_names("workflow_settings"):
        with op.batch_alter_table("workflow_settings") as batch_op:
            batch_op.drop_column("grpc_audio_workflow_id")

    if "grpc_max_sessions" in _column_names("capacity_settings"):
        with op.batch_alter_table("capacity_settings") as batch_op:
            batch_op.drop_column("grpc_max_sessions")


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("workflow_settings"):
        with op.batch_alter_table("workflow_settings") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "grpc_audio_workflow_id",
                    sa.String(length=100),
                    nullable=True,
                )
            )
    if inspector.has_table("capacity_settings"):
        with op.batch_alter_table("capacity_settings") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "grpc_max_sessions",
                    sa.Integer(),
                    nullable=False,
                    server_default="64",
                )
            )
