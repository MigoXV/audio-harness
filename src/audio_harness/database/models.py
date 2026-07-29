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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from audio_harness.assets import (
    EngineApi,
    EngineKind,
    EngineProtocol,
    EngineRole,
    EngineStatus,
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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[EngineStatus] = mapped_column(enum_type(EngineStatus))
    image: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    status_basis: Mapped[str | None] = mapped_column(Text)
    status_observed_at: Mapped[datetime | None] = mapped_column(
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
    bindings: Mapped[list["EngineBinding"]] = relationship(
        back_populates="engine",
        passive_deletes=True,
    )


class EngineBinding(Base):
    __tablename__ = "engine_bindings"

    role: Mapped[EngineRole] = mapped_column(
        enum_type(EngineRole),
        primary_key=True,
    )
    engine_id: Mapped[str] = mapped_column(
        ForeignKey("engine_assets.id", ondelete="RESTRICT"),
        index=True,
    )
    generation: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )
    engine: Mapped[EngineAsset] = relationship(back_populates="bindings")
