from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from audio_harness.assets import (
    EngineApi,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.workflow_types import (
    ModelRouteVisibility,
    WorkflowKind,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def enum_type(enum_class: type) -> SqlEnum:
    return SqlEnum(
        enum_class,
        native_enum=False,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(DeclarativeBase):
    pass


class EngineAsset(Base):
    __tablename__ = "engine_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[EngineKind] = mapped_column(enum_type(EngineKind))
    api: Mapped[EngineApi] = mapped_column(enum_type(EngineApi))
    protocol: Mapped[EngineProtocol] = mapped_column(
        enum_type(EngineProtocol),
        default=EngineProtocol.GRPC,
    )
    target: Mapped[str] = mapped_column(String(500))
    connect_timeout_seconds: Mapped[float] = mapped_column(Float, default=10.0)
    request_timeout_seconds: Mapped[float | None] = mapped_column(Float)
    credential_env: Mapped[str | None] = mapped_column(String(200))
    api_key: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[EngineStatus] = mapped_column(enum_type(EngineStatus))
    image: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    status_basis: Mapped[str | None] = mapped_column(Text)
    status_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    probe_latency_ms: Mapped[float | None] = mapped_column(Float)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    next_probe_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    source: Mapped[str | None] = mapped_column(String(500))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class WorkflowSetting(Base):
    __tablename__ = "workflow_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    min_chunk_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    default_language_code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    audio_queue_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_queue_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    result_queue_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class CapacitySetting(Base):
    __tablename__ = "capacity_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    grpc_max_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    realtime_max_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    webrtc_max_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    max_asr_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    max_responses_concurrency: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    max_tts_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class PipelineWorkingCopy(Base):
    __tablename__ = "pipeline_working_copy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class PipelineRevision(Base):
    __tablename__ = "pipeline_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    compiled_plan: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


class PipelineActivation(Base):
    __tablename__ = "pipeline_activation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    active_revision: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_revisions.revision", ondelete="RESTRICT"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[WorkflowKind] = mapped_column(enum_type(WorkflowKind))
    description: Mapped[str | None] = mapped_column(Text)
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    draft_definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class WorkflowRevision(Base):
    __tablename__ = "workflow_revisions"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id",
            "revision",
            name="uq_workflow_revision_number",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    compiled_plan: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


class WorkflowActivation(Base):
    __tablename__ = "workflow_activations"

    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"),
        primary_key=True,
    )
    active_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_revisions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class ModelRoute(Base):
    __tablename__ = "model_routes"

    model: Mapped[str] = mapped_column(String(100), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("workflows.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    visibility: Mapped[ModelRouteVisibility] = mapped_column(
        enum_type(ModelRouteVisibility)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class VoiceAsset(Base):
    __tablename__ = "voice_assets"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    wav_path: Mapped[str] = mapped_column(String(500), unique=True)
    ref_text: Mapped[str | None] = mapped_column(Text)
    sample_rate: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
