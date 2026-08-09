from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.assets import EngineAssetData, EngineSnapshot
from audio_harness.database.mappers import asset_data
from audio_harness.database.models import (
    EngineAsset,
    RealtimeRoute,
    VoiceAgentActivation,
    VoiceAgentProfile,
    VoiceAgentRevision,
    utc_now,
)
from audio_harness.voice_agents.models import VoiceAgentDefinition
from audio_harness.workflow_types import RealtimeRouteTargetType


class VoiceAgentRepositoryError(Exception):
    pass


class VoiceAgentNotFoundError(VoiceAgentRepositoryError):
    pass


class VoiceAgentConflictError(VoiceAgentRepositoryError):
    pass


@dataclass(frozen=True)
class VoiceAgentSummary:
    id: str
    slug: str
    name: str
    description: str | None
    draft_revision: int
    active_revision: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class VoiceAgentDraft:
    id: str
    slug: str
    name: str
    description: str | None
    definition: VoiceAgentDefinition
    revision: int
    active_revision: int | None
    updated_at: datetime


@dataclass(frozen=True)
class StoredVoiceAgentRevision:
    id: int
    voice_agent_id: str
    revision: int
    definition: VoiceAgentDefinition
    compiled_profile: dict[str, Any]
    audio_workflow_revision_id: int
    activated_at: datetime


class VoiceAgentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def engine_assets(self) -> dict[str, EngineAssetData]:
        async with self._session_factory() as session:
            models = (await session.execute(select(EngineAsset))).scalars().all()
        return {model.id: asset_data(model) for model in models}

    async def list(self) -> tuple[VoiceAgentSummary, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(VoiceAgentProfile, VoiceAgentRevision.revision)
                    .outerjoin(
                        VoiceAgentActivation,
                        VoiceAgentActivation.voice_agent_id == VoiceAgentProfile.id,
                    )
                    .outerjoin(
                        VoiceAgentRevision,
                        VoiceAgentRevision.id == VoiceAgentActivation.active_revision_id,
                    )
                    .order_by(VoiceAgentProfile.created_at, VoiceAgentProfile.id)
                )
            ).all()
        return tuple(self._summary(profile, active) for profile, active in rows)

    async def get_summary(self, profile_id: str) -> VoiceAgentSummary:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(VoiceAgentProfile, VoiceAgentRevision.revision)
                    .outerjoin(
                        VoiceAgentActivation,
                        VoiceAgentActivation.voice_agent_id == VoiceAgentProfile.id,
                    )
                    .outerjoin(
                        VoiceAgentRevision,
                        VoiceAgentRevision.id == VoiceAgentActivation.active_revision_id,
                    )
                    .where(VoiceAgentProfile.id == profile_id)
                )
            ).one_or_none()
        if row is None:
            raise VoiceAgentNotFoundError(f"语音智能体不存在: {profile_id}")
        return self._summary(row[0], row[1])

    async def create(
        self,
        *,
        slug: str,
        name: str,
        description: str | None,
        definition: VoiceAgentDefinition,
    ) -> VoiceAgentSummary:
        now = utc_now()
        profile = VoiceAgentProfile(
            id=str(uuid4()),
            slug=slug,
            name=name,
            description=description,
            draft_revision=1,
            draft_definition=definition.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )
        activation = VoiceAgentActivation(
            voice_agent_id=profile.id,
            active_revision_id=None,
            updated_at=now,
        )
        try:
            async with self._session_factory() as session:
                session.add(profile)
                await session.flush()
                session.add(activation)
                await session.commit()
        except IntegrityError as exc:
            raise VoiceAgentConflictError(f"语音智能体 slug 已存在: {slug}") from exc
        return self._summary(profile, None)

    async def get_draft(self, profile_id: str) -> VoiceAgentDraft:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(VoiceAgentProfile, VoiceAgentRevision.revision)
                    .outerjoin(
                        VoiceAgentActivation,
                        VoiceAgentActivation.voice_agent_id == VoiceAgentProfile.id,
                    )
                    .outerjoin(
                        VoiceAgentRevision,
                        VoiceAgentRevision.id == VoiceAgentActivation.active_revision_id,
                    )
                    .where(VoiceAgentProfile.id == profile_id)
                )
            ).one_or_none()
        if row is None:
            raise VoiceAgentNotFoundError(f"语音智能体不存在: {profile_id}")
        profile, active = row
        return VoiceAgentDraft(
            id=profile.id,
            slug=profile.slug,
            name=profile.name,
            description=profile.description,
            definition=VoiceAgentDefinition.model_validate(profile.draft_definition),
            revision=profile.draft_revision,
            active_revision=active,
            updated_at=profile.updated_at,
        )

    async def update_metadata(
        self,
        profile_id: str,
        *,
        name: str | None,
        description: str | None,
    ) -> VoiceAgentSummary:
        async with self._session_factory() as session:
            profile = await session.get(VoiceAgentProfile, profile_id)
            if profile is None:
                raise VoiceAgentNotFoundError(f"语音智能体不存在: {profile_id}")
            if name is not None:
                profile.name = name
            if description is not None:
                profile.description = description
            profile.updated_at = utc_now()
            await session.commit()
        return await self.get_summary(profile_id)

    async def update_draft(
        self,
        profile_id: str,
        *,
        definition: VoiceAgentDefinition,
        expected_revision: int,
    ) -> VoiceAgentDraft:
        async with self._session_factory() as session:
            profile = await session.get(VoiceAgentProfile, profile_id)
            if profile is None:
                raise VoiceAgentNotFoundError(f"语音智能体不存在: {profile_id}")
            if profile.draft_revision != expected_revision:
                raise VoiceAgentConflictError("语音智能体草稿已经被其他操作更新")
            profile.draft_definition = definition.model_dump(mode="json")
            profile.draft_revision += 1
            profile.updated_at = utc_now()
            await session.commit()
        return await self.get_draft(profile_id)

    async def activate(
        self,
        profile_id: str,
        *,
        expected_revision: int,
        definition: VoiceAgentDefinition,
        compiled_profile: dict[str, Any],
        audio_workflow_revision_id: int,
        engine_snapshots: tuple[EngineSnapshot, ...],
    ) -> StoredVoiceAgentRevision:
        now = utc_now()
        async with self._session_factory() as session:
            profile = await session.get(VoiceAgentProfile, profile_id)
            if profile is None:
                raise VoiceAgentNotFoundError(f"语音智能体不存在: {profile_id}")
            if profile.draft_revision != expected_revision:
                raise VoiceAgentConflictError("语音智能体草稿已经被其他操作更新")
            for snapshot in engine_snapshots:
                asset = await session.get(EngineAsset, snapshot.id)
                if asset is None or asset.revision != snapshot.revision or not asset.enabled:
                    raise VoiceAgentConflictError("语音智能体引用的引擎在验证期间发生变化")
            latest = await session.scalar(
                select(func.max(VoiceAgentRevision.revision)).where(
                    VoiceAgentRevision.voice_agent_id == profile_id
                )
            )
            revision = VoiceAgentRevision(
                voice_agent_id=profile_id,
                revision=int(latest or 0) + 1,
                definition=definition.model_dump(mode="json"),
                compiled_profile=compiled_profile,
                audio_workflow_revision_id=audio_workflow_revision_id,
                created_at=now,
                activated_at=now,
            )
            session.add(revision)
            await session.flush()
            activation = await session.get(VoiceAgentActivation, profile_id)
            if activation is None:
                raise RuntimeError("语音智能体激活记录不存在")
            activation.active_revision_id = revision.id
            activation.updated_at = now
            await session.commit()
            await session.refresh(revision)
        return self._revision(revision)

    async def active_revisions(self) -> tuple[StoredVoiceAgentRevision, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(VoiceAgentRevision)
                    .join(
                        VoiceAgentActivation,
                        VoiceAgentActivation.active_revision_id == VoiceAgentRevision.id,
                    )
                    .order_by(VoiceAgentRevision.voice_agent_id)
                )
            ).scalars().all()
        return tuple(self._revision(row) for row in rows)

    async def get_revision(
        self,
        profile_id: str,
        revision_number: int,
    ) -> StoredVoiceAgentRevision:
        async with self._session_factory() as session:
            revision = (
                await session.execute(
                    select(VoiceAgentRevision).where(
                        VoiceAgentRevision.voice_agent_id == profile_id,
                        VoiceAgentRevision.revision == revision_number,
                    )
                )
            ).scalar_one_or_none()
        if revision is None:
            raise VoiceAgentNotFoundError(
                f"语音智能体修订不存在: {profile_id}/{revision_number}"
            )
        return self._revision(revision)

    async def list_revisions(
        self,
        profile_id: str,
    ) -> tuple[StoredVoiceAgentRevision, ...]:
        await self.get_summary(profile_id)
        async with self._session_factory() as session:
            revisions = (
                await session.execute(
                    select(VoiceAgentRevision)
                    .where(VoiceAgentRevision.voice_agent_id == profile_id)
                    .order_by(VoiceAgentRevision.revision.desc())
                )
            ).scalars().all()
        return tuple(self._revision(revision) for revision in revisions)

    async def restore_as_draft(
        self,
        profile_id: str,
        revision_number: int,
        *,
        expected_revision: int,
    ) -> VoiceAgentDraft:
        revision = await self.get_revision(profile_id, revision_number)
        return await self.update_draft(
            profile_id,
            definition=revision.definition,
            expected_revision=expected_revision,
        )

    async def delete(self, profile_id: str) -> None:
        async with self._session_factory() as session:
            profile = await session.get(VoiceAgentProfile, profile_id)
            if profile is None:
                raise VoiceAgentNotFoundError(f"语音智能体不存在: {profile_id}")
            route = await session.scalar(
                select(RealtimeRoute.model).where(
                    RealtimeRoute.target_type == RealtimeRouteTargetType.VOICE_AGENT,
                    RealtimeRoute.target_id == profile_id,
                )
            )
            if route is not None:
                raise VoiceAgentConflictError(
                    f"语音智能体仍被 Realtime Route {route} 引用"
                )
            await session.execute(
                delete(VoiceAgentActivation).where(
                    VoiceAgentActivation.voice_agent_id == profile_id
                )
            )
            await session.execute(
                delete(VoiceAgentRevision).where(
                    VoiceAgentRevision.voice_agent_id == profile_id
                )
            )
            await session.delete(profile)
            await session.commit()

    @staticmethod
    def _summary(profile: VoiceAgentProfile, active: int | None) -> VoiceAgentSummary:
        return VoiceAgentSummary(
            id=profile.id,
            slug=profile.slug,
            name=profile.name,
            description=profile.description,
            draft_revision=profile.draft_revision,
            active_revision=active,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )

    @staticmethod
    def _revision(revision: VoiceAgentRevision) -> StoredVoiceAgentRevision:
        return StoredVoiceAgentRevision(
            id=revision.id,
            voice_agent_id=revision.voice_agent_id,
            revision=revision.revision,
            definition=VoiceAgentDefinition.model_validate(revision.definition),
            compiled_profile=dict(revision.compiled_profile),
            audio_workflow_revision_id=revision.audio_workflow_revision_id,
            activated_at=revision.activated_at,
        )
