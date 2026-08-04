"""将固定角色绑定迁移为单一默认 DAG。

Revision ID: 0006_pipeline_dag
Revises: 0005_reduce_input_chunk_size
Create Date: 2026-07-31
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0006_pipeline_dag"
down_revision = "0005_reduce_input_chunk_size"
branch_labels = None
depends_on = None


def _definition(
    bindings: dict[str, str],
    workflow: dict[str, Any],
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [
        {
            "id": "audio_input",
            "type": "audio_input",
            "display_name": None,
            "engine_id": None,
            "config": {},
            "failure_policy": None,
            "position": {"x": 80, "y": 220},
        },
        {
            "id": "physical_vad",
            "type": "physical_vad",
            "display_name": None,
            "engine_id": bindings.get("vad"),
            "config": {
                "min_segment_seconds": workflow["min_segment_seconds"],
                "max_segment_seconds": workflow["max_segment_seconds"],
                "audio_buffer_seconds": workflow["audio_buffer_seconds"],
                "speech_pre_roll_seconds": workflow[
                    "speech_pre_roll_seconds"
                ],
            },
            "failure_policy": None,
            "position": {"x": 310, "y": 220},
        },
        {
            "id": "asr",
            "type": "asr",
            "display_name": None,
            "engine_id": bindings.get("asr"),
            "config": {},
            "failure_policy": None,
            "position": {"x": 760, "y": 220},
        },
        {
            "id": "session_output",
            "type": "session_output",
            "display_name": None,
            "engine_id": None,
            "config": {},
            "failure_policy": None,
            "position": {"x": 1000, "y": 220},
        },
    ]
    segment_source = "physical_vad"
    if bindings.get("semantic_vad"):
        nodes.insert(
            2,
            {
                "id": "semantic_vad",
                "type": "semantic_vad",
                "display_name": None,
                "engine_id": bindings["semantic_vad"],
                "config": {},
                "failure_policy": "fail_open",
                "position": {"x": 535, "y": 220},
            },
        )
        segment_source = "semantic_vad"

    edges: list[dict[str, Any]] = [
        {
            "id": "audio-input-to-vad",
            "source_node": "audio_input",
            "source_port": "audio",
            "target_node": "physical_vad",
            "target_port": "audio",
        },
        {
            "id": "segments-to-asr",
            "source_node": segment_source,
            "source_port": "segments",
            "target_node": "asr",
            "target_port": "segments",
        },
        {
            "id": "vad-events-to-output",
            "source_node": "physical_vad",
            "source_port": "events",
            "target_node": "session_output",
            "target_port": "events",
        },
        {
            "id": "asr-events-to-output",
            "source_node": "asr",
            "source_port": "events",
            "target_node": "session_output",
            "target_port": "events",
        },
    ]
    if segment_source == "semantic_vad":
        edges.extend(
            [
                {
                    "id": "vad-to-semantic-vad",
                    "source_node": "physical_vad",
                    "source_port": "segments",
                    "target_node": "semantic_vad",
                    "target_port": "segments",
                },
                {
                    "id": "semantic-events-to-output",
                    "source_node": "semantic_vad",
                    "source_port": "events",
                    "target_node": "session_output",
                    "target_port": "events",
                },
            ]
        )
    return {"schema_version": 1, "nodes": nodes, "edges": edges}


def _compiled_plan(
    definition: dict[str, Any],
    assets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    order = ["audio_input", "physical_vad"]
    if any(node["type"] == "semantic_vad" for node in definition["nodes"]):
        order.append("semantic_vad")
    order.extend(["asr", "session_output"])
    snapshots: dict[str, Any] = {}
    for node in definition["nodes"]:
        engine_id = node.get("engine_id")
        if engine_id is None:
            continue
        asset = assets[engine_id]
        snapshots[node["id"]] = {
            "id": asset["id"],
            "revision": asset["revision"],
            "name": asset["name"],
            "api": asset["api"],
            "protocol": asset["protocol"],
            "target": asset["target"],
            "connect_timeout_seconds": asset[
                "connect_timeout_seconds"
            ],
            "request_timeout_seconds": asset[
                "request_timeout_seconds"
            ],
        }
    return {
        "schema_version": 1,
        "topological_order": order,
        "node_type_versions": {
            node["id"]: 1 for node in definition["nodes"]
        },
        "engine_snapshots": snapshots,
    }


def upgrade() -> None:
    op.create_table(
        "pipeline_working_copy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_table(
        "pipeline_revisions",
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("compiled_plan", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_table(
        "pipeline_activation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "active_revision",
            sa.Integer(),
            sa.ForeignKey(
                "pipeline_revisions.revision",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )

    connection = op.get_bind()
    bindings = {
        row.role: row.engine_id
        for row in connection.execute(
            sa.text("SELECT role, engine_id FROM engine_bindings")
        ).mappings()
    }
    workflow = dict(
        connection.execute(
            sa.text(
                "SELECT min_segment_seconds, max_segment_seconds, "
                "audio_buffer_seconds, speech_pre_roll_seconds "
                "FROM workflow_settings WHERE id = 1"
            )
        ).mappings().one()
    )
    definition = _definition(bindings, workflow)
    now = datetime.now(timezone.utc)
    working = sa.table(
        "pipeline_working_copy",
        sa.column("id"),
        sa.column("revision"),
        sa.column("definition", sa.JSON()),
        sa.column("updated_at"),
    )
    connection.execute(
        working.insert().values(
            id=1,
            revision=1,
            definition=definition,
            updated_at=now,
        )
    )

    active_revision: int | None = None
    if bindings.get("asr") and bindings.get("vad"):
        asset_ids = {
            node["engine_id"]
            for node in definition["nodes"]
            if node.get("engine_id")
        }
        rows = connection.execute(
            sa.text(
                "SELECT id, revision, name, api, protocol, target, enabled, "
                "connect_timeout_seconds, request_timeout_seconds "
                "FROM engine_assets"
            )
        ).mappings()
        assets = {
            str(row["id"]): dict(row)
            for row in rows
            if str(row["id"]) in asset_ids
        }
        expected_api = {
            "physical_vad": "ux_vad",
            "semantic_vad": "ux_vad",
            "asr": "ux_speech",
        }
        assets_valid = asset_ids.issubset(assets) and all(
            assets[node["engine_id"]]["enabled"]
            and assets[node["engine_id"]]["api"]
            == expected_api[node["type"]]
            for node in definition["nodes"]
            if node.get("engine_id")
        )
        if assets_valid:
            revisions = sa.table(
                "pipeline_revisions",
                sa.column("revision"),
                sa.column("definition", sa.JSON()),
                sa.column("compiled_plan", sa.JSON()),
                sa.column("created_at"),
                sa.column("activated_at"),
            )
            connection.execute(
                revisions.insert().values(
                    revision=1,
                    definition=definition,
                    compiled_plan=_compiled_plan(definition, assets),
                    created_at=now,
                    activated_at=now,
                )
            )
            active_revision = 1

    activation = sa.table(
        "pipeline_activation",
        sa.column("id"),
        sa.column("active_revision"),
        sa.column("updated_at"),
    )
    connection.execute(
        activation.insert().values(
            id=1,
            active_revision=active_revision,
            updated_at=now,
        )
    )
    op.drop_index("ix_engine_bindings_engine_id", table_name="engine_bindings")
    op.drop_table("engine_bindings")
    with op.batch_alter_table("workflow_settings") as batch_op:
        batch_op.drop_column("min_segment_seconds")
        batch_op.drop_column("max_segment_seconds")
        batch_op.drop_column("audio_buffer_seconds")
        batch_op.drop_column("speech_pre_roll_seconds")


def downgrade() -> None:
    with op.batch_alter_table("workflow_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "min_segment_seconds",
                sa.Float(),
                nullable=False,
                server_default="0.5",
            )
        )
        batch_op.add_column(
            sa.Column(
                "max_segment_seconds",
                sa.Float(),
                nullable=False,
                server_default="60.0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "audio_buffer_seconds",
                sa.Float(),
                nullable=False,
                server_default="120.0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "speech_pre_roll_seconds",
                sa.Float(),
                nullable=False,
                server_default="0.3",
            )
        )

    op.create_table(
        "engine_bindings",
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("engine_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, default=1),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
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
    )
    connection = op.get_bind()
    working = connection.execute(
        sa.text(
            "SELECT definition FROM pipeline_working_copy WHERE id = 1"
        )
    ).scalar_one()
    if isinstance(working, str):
        working = json.loads(working)
    now = datetime.now(timezone.utc)
    rows = []
    role_by_type = {
        "asr": "asr",
        "physical_vad": "vad",
        "semantic_vad": "semantic_vad",
    }
    for node in working.get("nodes", []):
        role = role_by_type.get(node.get("type"))
        engine_id = node.get("engine_id")
        if role and engine_id:
            rows.append(
                {
                    "role": role,
                    "engine_id": engine_id,
                    "generation": 1,
                    "updated_at": now,
                }
            )
        if node.get("type") == "physical_vad":
            config = node.get("config") or {}
            connection.execute(
                sa.text(
                    "UPDATE workflow_settings SET "
                    "min_segment_seconds = :min_segment_seconds, "
                    "max_segment_seconds = :max_segment_seconds, "
                    "audio_buffer_seconds = :audio_buffer_seconds, "
                    "speech_pre_roll_seconds = :speech_pre_roll_seconds "
                    "WHERE id = 1"
                ),
                {
                    "min_segment_seconds": config.get(
                        "min_segment_seconds",
                        0.5,
                    ),
                    "max_segment_seconds": config.get(
                        "max_segment_seconds",
                        60.0,
                    ),
                    "audio_buffer_seconds": config.get(
                        "audio_buffer_seconds",
                        120.0,
                    ),
                    "speech_pre_roll_seconds": config.get(
                        "speech_pre_roll_seconds",
                        0.3,
                    ),
                },
            )
    if rows:
        bindings = sa.table(
            "engine_bindings",
            sa.column("role"),
            sa.column("engine_id"),
            sa.column("generation"),
            sa.column("updated_at"),
        )
        connection.execute(bindings.insert(), rows)

    op.drop_table("pipeline_activation")
    op.drop_table("pipeline_revisions")
    op.drop_table("pipeline_working_copy")
