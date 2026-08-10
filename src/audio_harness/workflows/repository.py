from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.assets import EngineSnapshot
from audio_harness.database.models import (
    AudioWorkflow,
    AudioWorkflowActivation,
    AudioWorkflowRevision,
    EngineAsset,
    RealtimeRoute,
    VoiceAgentProfile,
    VoiceAgentRevision,
    WorkflowSetting,
    utc_now,
)
from audio_harness.database.mappers import asset_data
from audio_harness.workflows.models import AudioWorkflowDocument, default_audio_workflow
from audio_harness.workflow_types import RealtimeRouteTargetType


class AudioWorkflowRepositoryError(Exception):
    pass


class AudioWorkflowNotFoundError(AudioWorkflowRepositoryError):
    pass


class AudioWorkflowConflictError(AudioWorkflowRepositoryError):
    pass


class AudioWorkflowRevisionNotFoundError(AudioWorkflowRepositoryError):
    pass


@dataclass(frozen=True)
class AudioWorkflowSummary:
    id: str
    slug: str
    name: str
    description: str | None
    draft_revision: int
    active_revision: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AudioWorkflowDraft:
    id: str
    slug: str
    name: str
    description: str | None
    document: AudioWorkflowDocument
    revision: int
    active_revision: int | None
    updated_at: datetime


@dataclass(frozen=True)
class StoredAudioWorkflowRevision:
    id: int
    audio_workflow_id: str
    revision: int
    document: AudioWorkflowDocument
    compiled_plan: dict[str, Any]
    created_at: datetime
    activated_at: datetime


class AudioWorkflowRepository:
    """语音工作流的 SQLite 持久化边界。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def engine_assets(self) -> dict[str, Any]:
        async with self._session_factory() as session:
            models = (await session.execute(select(EngineAsset))).scalars().all()
        return {model.id: asset_data(model) for model in models}

    async def list(self) -> tuple[AudioWorkflowSummary, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(AudioWorkflow, AudioWorkflowRevision.revision)
                    .outerjoin(
                        AudioWorkflowActivation,
                        AudioWorkflowActivation.audio_workflow_id == AudioWorkflow.id,
                    )
                    .outerjoin(
                        AudioWorkflowRevision,
                        AudioWorkflowRevision.id
                        == AudioWorkflowActivation.active_revision_id,
                    )
                    .order_by(AudioWorkflow.created_at, AudioWorkflow.id)
                )
            ).all()
        return tuple(self._summary(workflow, active_revision) for workflow, active_revision in rows)

    async def get_summary(self, workflow_id: str) -> AudioWorkflowSummary:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AudioWorkflow, AudioWorkflowRevision.revision)
                    .outerjoin(
                        AudioWorkflowActivation,
                        AudioWorkflowActivation.audio_workflow_id == AudioWorkflow.id,
                    )
                    .outerjoin(
                        AudioWorkflowRevision,
                        AudioWorkflowRevision.id
                        == AudioWorkflowActivation.active_revision_id,
                    )
                    .where(AudioWorkflow.id == workflow_id)
                )
            ).one_or_none()
        if row is None:
            raise AudioWorkflowNotFoundError(f"语音工作流不存在: {workflow_id}")
        return self._summary(row[0], row[1])

    async def create(
        self,
        *,
        slug: str,
        name: str,
        description: str | None,
        document: AudioWorkflowDocument | None = None,
    ) -> AudioWorkflowSummary:
        now = utc_now()
        workflow = AudioWorkflow(
            id=str(uuid4()),
            slug=slug,
            name=name,
            description=description,
            draft_revision=1,
            draft_document=(document or default_audio_workflow()).model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )
        activation = AudioWorkflowActivation(
            audio_workflow_id=workflow.id,
            active_revision_id=None,
            updated_at=now,
        )
        try:
            async with self._session_factory() as session:
                session.add(workflow)
                await session.flush()
                session.add(activation)
                await session.commit()
        except IntegrityError as exc:
            raise AudioWorkflowConflictError(f"语音工作流 slug 已存在: {slug}") from exc
        return self._summary(workflow, None)

    async def get_draft(self, workflow_id: str) -> AudioWorkflowDraft:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AudioWorkflow, AudioWorkflowRevision.revision)
                    .outerjoin(
                        AudioWorkflowActivation,
                        AudioWorkflowActivation.audio_workflow_id == AudioWorkflow.id,
                    )
                    .outerjoin(
                        AudioWorkflowRevision,
                        AudioWorkflowRevision.id
                        == AudioWorkflowActivation.active_revision_id,
                    )
                    .where(AudioWorkflow.id == workflow_id)
                )
            ).one_or_none()
        if row is None:
            raise AudioWorkflowNotFoundError(f"语音工作流不存在: {workflow_id}")
        workflow, active_revision = row
        return AudioWorkflowDraft(
            id=workflow.id,
            slug=workflow.slug,
            name=workflow.name,
            description=workflow.description,
            document=AudioWorkflowDocument.model_validate(workflow.draft_document),
            revision=workflow.draft_revision,
            active_revision=active_revision,
            updated_at=workflow.updated_at,
        )

    async def update_draft(
        self,
        workflow_id: str,
        *,
        document: AudioWorkflowDocument,
        expected_revision: int,
    ) -> AudioWorkflowDraft:
        async with self._session_factory() as session:
            workflow = await session.get(AudioWorkflow, workflow_id)
            if workflow is None:
                raise AudioWorkflowNotFoundError(f"语音工作流不存在: {workflow_id}")
            if workflow.draft_revision != expected_revision:
                raise AudioWorkflowConflictError("语音工作流草稿已经被其他操作更新")
            workflow.draft_document = document.model_dump(mode="json")
            workflow.draft_revision += 1
            workflow.updated_at = utc_now()
            await session.commit()
        return await self.get_draft(workflow_id)

    async def update_metadata(
        self,
        workflow_id: str,
        *,
        name: str | None,
        description: str | None,
    ) -> AudioWorkflowSummary:
        async with self._session_factory() as session:
            workflow = await session.get(AudioWorkflow, workflow_id)
            if workflow is None:
                raise AudioWorkflowNotFoundError(f"语音工作流不存在: {workflow_id}")
            if name is not None:
                workflow.name = name
            workflow.description = description
            workflow.updated_at = utc_now()
            await session.commit()
        return await self.get_summary(workflow_id)

    async def activate(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        document: AudioWorkflowDocument,
        compiled_plan: dict[str, Any],
        engine_snapshots: tuple[EngineSnapshot, ...],
    ) -> StoredAudioWorkflowRevision:
        now = utc_now()
        async with self._session_factory() as session:
            workflow = await session.get(AudioWorkflow, workflow_id)
            if workflow is None:
                raise AudioWorkflowNotFoundError(f"语音工作流不存在: {workflow_id}")
            if workflow.draft_revision != expected_revision:
                raise AudioWorkflowConflictError("语音工作流草稿已经被其他操作更新")
            for snapshot in engine_snapshots:
                asset = await session.get(EngineAsset, snapshot.id)
                if asset is None or asset.revision != snapshot.revision or not asset.enabled:
                    raise AudioWorkflowConflictError("工作流引用的引擎在验证期间发生变化")
            latest = await session.scalar(
                select(func.max(AudioWorkflowRevision.revision)).where(
                    AudioWorkflowRevision.audio_workflow_id == workflow_id
                )
            )
            revision = AudioWorkflowRevision(
                audio_workflow_id=workflow_id,
                revision=int(latest or 0) + 1,
                document=document.model_dump(mode="json"),
                compiled_plan=compiled_plan,
                created_at=now,
                activated_at=now,
            )
            session.add(revision)
            await session.flush()
            activation = await session.get(AudioWorkflowActivation, workflow_id)
            if activation is None:
                raise RuntimeError("语音工作流激活记录不存在")
            activation.active_revision_id = revision.id
            activation.updated_at = now
            await session.commit()
            await session.refresh(revision)
        return self._revision(revision)

    async def active_revisions(self) -> tuple[StoredAudioWorkflowRevision, ...]:
        async with self._session_factory() as session:
            revisions = (
                await session.execute(
                    select(AudioWorkflowRevision)
                    .join(
                        AudioWorkflowActivation,
                        AudioWorkflowActivation.active_revision_id
                        == AudioWorkflowRevision.id,
                    )
                    .order_by(AudioWorkflowRevision.audio_workflow_id)
                )
            ).scalars().all()
        return tuple(self._revision(revision) for revision in revisions)

    async def get_revision(
        self,
        workflow_id: str,
        revision_number: int,
    ) -> StoredAudioWorkflowRevision:
        async with self._session_factory() as session:
            revision = (
                await session.execute(
                    select(AudioWorkflowRevision).where(
                        AudioWorkflowRevision.audio_workflow_id == workflow_id,
                        AudioWorkflowRevision.revision == revision_number,
                    )
                )
            ).scalar_one_or_none()
        if revision is None:
            raise AudioWorkflowRevisionNotFoundError(
                f"语音工作流修订不存在: {workflow_id}/{revision_number}"
            )
        return self._revision(revision)

    async def get_revision_by_id(self, revision_id: int) -> StoredAudioWorkflowRevision:
        async with self._session_factory() as session:
            revision = await session.get(AudioWorkflowRevision, revision_id)
        if revision is None:
            raise AudioWorkflowRevisionNotFoundError(
                f"语音工作流修订不存在: {revision_id}"
            )
        return self._revision(revision)

    async def list_revisions(
        self,
        workflow_id: str,
    ) -> tuple[StoredAudioWorkflowRevision, ...]:
        await self.get_summary(workflow_id)
        async with self._session_factory() as session:
            revisions = (
                await session.execute(
                    select(AudioWorkflowRevision)
                    .where(AudioWorkflowRevision.audio_workflow_id == workflow_id)
                    .order_by(AudioWorkflowRevision.revision.desc())
                )
            ).scalars().all()
        return tuple(self._revision(revision) for revision in revisions)

    async def restore_as_draft(
        self,
        workflow_id: str,
        revision_number: int,
        *,
        expected_revision: int,
    ) -> AudioWorkflowDraft:
        revision = await self.get_revision(workflow_id, revision_number)
        return await self.update_draft(
            workflow_id,
            document=revision.document,
            expected_revision=expected_revision,
        )

    async def delete(self, workflow_id: str) -> None:
        async with self._session_factory() as session:
            workflow = await session.get(AudioWorkflow, workflow_id)
            if workflow is None:
                raise AudioWorkflowNotFoundError(f"语音工作流不存在: {workflow_id}")
            references: list[str] = []
            settings = await session.get(WorkflowSetting, 1)
            if settings is not None and settings.grpc_audio_workflow_id == workflow_id:
                references.append("gRPC 默认绑定")
            route = await session.scalar(
                select(RealtimeRoute.model).where(
                    RealtimeRoute.target_type
                    == RealtimeRouteTargetType.AUDIO_WORKFLOW,
                    RealtimeRoute.target_id == workflow_id,
                )
            )
            if route is not None:
                references.append(f"Realtime Route {route}")
            profiles = (
                await session.execute(select(VoiceAgentProfile))
            ).scalars().all()
            references.extend(
                f"语音智能体草稿 {profile.name}"
                for profile in profiles
                if profile.draft_definition.get("audio_workflow_id") == workflow_id
            )
            active_agent = await session.scalar(
                select(VoiceAgentRevision.voice_agent_id)
                .join(
                    AudioWorkflowRevision,
                    AudioWorkflowRevision.id
                    == VoiceAgentRevision.audio_workflow_revision_id,
                )
                .where(AudioWorkflowRevision.audio_workflow_id == workflow_id)
                .limit(1)
            )
            if active_agent is not None:
                references.append(f"语音智能体发布历史 {active_agent}")
            if references:
                raise AudioWorkflowConflictError(
                    "语音工作流仍被引用，不能删除: " + ", ".join(references)
                )
            await session.execute(
                delete(AudioWorkflowActivation).where(
                    AudioWorkflowActivation.audio_workflow_id == workflow_id
                )
            )
            await session.delete(workflow)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise AudioWorkflowConflictError("语音工作流仍被语音智能体或路由引用") from exc

    @staticmethod
    def _summary(
        workflow: AudioWorkflow,
        active_revision: int | None,
    ) -> AudioWorkflowSummary:
        return AudioWorkflowSummary(
            id=workflow.id,
            slug=workflow.slug,
            name=workflow.name,
            description=workflow.description,
            draft_revision=workflow.draft_revision,
            active_revision=active_revision,
            created_at=workflow.created_at,
            updated_at=workflow.updated_at,
        )

    @staticmethod
    def _revision(revision: AudioWorkflowRevision) -> StoredAudioWorkflowRevision:
        return StoredAudioWorkflowRevision(
            id=revision.id,
            audio_workflow_id=revision.audio_workflow_id,
            revision=revision.revision,
            document=AudioWorkflowDocument.model_validate(revision.document),
            compiled_plan=dict(revision.compiled_plan),
            created_at=revision.created_at,
            activated_at=revision.activated_at,
        )
