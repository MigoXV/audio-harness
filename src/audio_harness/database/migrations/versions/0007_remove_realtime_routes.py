"""以已发布资源 slug 替代 Realtime Route。"""

from alembic import op
import sqlalchemy as sa


revision = "0007_remove_realtime_routes"
down_revision = "0006_agent_backends"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT r.model, r.target_type, r.target_id, "
            "COALESCE(w.slug, a.slug) AS slug "
            "FROM realtime_routes r "
            "LEFT JOIN audio_workflows w ON r.target_type = 'audio_workflow' AND w.id = r.target_id "
            "LEFT JOIN voice_agent_profiles a ON r.target_type = 'voice_agent' AND a.id = r.target_id"
        )
    ).mappings()
    for row in rows:
        if row["slug"] is None or row["model"] != row["slug"]:
            raise RuntimeError(
                "无法移除 Realtime Route：存在非 slug 路由或目标资源不存在: "
                f"{row['model']}"
            )
    op.drop_index("ix_realtime_routes_target_id", table_name="realtime_routes")
    op.drop_table("realtime_routes")


def downgrade() -> None:
    raise RuntimeError("不支持恢复已移除的 Realtime Route")
