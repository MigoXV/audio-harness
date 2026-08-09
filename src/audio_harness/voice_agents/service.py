from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.assets import EngineApi, EngineProtocol, EngineSnapshot
from audio_harness.database.models import RealtimeRoute, utc_now
from audio_harness.engines.pool import InferencerPool
from audio_harness.voice_agents.models import VoiceAgentDefinition
from audio_harness.voice_agents.repository import (
    StoredVoiceAgentRevision,
    VoiceAgentConflictError,
    VoiceAgentDraft,
    VoiceAgentRepository,
    VoiceAgentSummary,
)
from audio_harness.workflow_types import (
    ModelRouteVisibility,
    RealtimeRouteTargetType,
)
from audio_harness.workflows.service import ActiveAudioWorkflow, AudioWorkflowService


class VoiceAgentNotActiveError(RuntimeError):
    pass


class VoiceAgentPublishError(RuntimeError):
    pass


class RealtimeRouteNotFoundError(RuntimeError):
    pass


class RealtimeRouteConflictError(RuntimeError):
    def __init__(self, message: str, *, current: "RealtimeRouteSnapshot") -> None:
        super().__init__(message)
        self.current = current


@dataclass(frozen=True)
class VoiceAgentDiagnostic:
    code: str
    message: str
    field: str | None = None


@dataclass(frozen=True)
class _VoiceAgentInputs:
    audio: ActiveAudioWorkflow
    responses: EngineSnapshot
    tts: EngineSnapshot


@dataclass(frozen=True)
class ActiveVoiceAgent:
    id: str
    revision: int
    definition: VoiceAgentDefinition
    audio_workflow: ActiveAudioWorkflow
    responses_engine: EngineSnapshot
    tts_engine: EngineSnapshot

    @property
    def engine_snapshots(self) -> tuple[EngineSnapshot, EngineSnapshot]:
        return (self.responses_engine, self.tts_engine)


def _snapshot_document(snapshot: EngineSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "revision": snapshot.revision,
        "name": snapshot.name,
        "api": snapshot.api.value,
        "protocol": snapshot.protocol.value,
        "target": snapshot.target,
        "connect_timeout_seconds": snapshot.connect_timeout_seconds,
        "request_timeout_seconds": snapshot.request_timeout_seconds,
        "credential_env": snapshot.credential_env,
        "api_key": snapshot.api_key,
        "details": snapshot.details,
    }


def _restore_snapshot(raw: object) -> EngineSnapshot:
    if not isinstance(raw, dict):
        raise ValueError("语音智能体执行计划缺少引擎快照")
    return EngineSnapshot(
        id=str(raw["id"]),
        revision=int(raw["revision"]),
        name=str(raw["name"]),
        api=EngineApi(str(raw["api"])),
        protocol=EngineProtocol(str(raw["protocol"])),
        target=str(raw["target"]),
        connect_timeout_seconds=float(raw["connect_timeout_seconds"]),
        request_timeout_seconds=(
            float(raw["request_timeout_seconds"])
            if raw.get("request_timeout_seconds") is not None
            else None
        ),
        credential_env=(
            str(raw["credential_env"])
            if raw.get("credential_env") is not None
            else None
        ),
        api_key=(str(raw["api_key"]) if raw.get("api_key") is not None else None),
        details=dict(raw.get("details") or {}),
    )


class VoiceAgentService:
    def __init__(
        self,
        repository: VoiceAgentRepository,
        *,
        audio_workflows: AudioWorkflowService,
        inferencer_pool: InferencerPool,
    ) -> None:
        self.repository = repository
        self.audio_workflows = audio_workflows
        self._inferencer_pool = inferencer_pool
        self._active: dict[str, ActiveVoiceAgent] = {}
        self._publish_lock = asyncio.Lock()

    async def initialize(self) -> None:
        active: dict[str, ActiveVoiceAgent] = {}
        for stored in await self.repository.active_revisions():
            value = await self._restore(stored)
            active[value.id] = value
        self._active = active

    def current(self, profile_id: str) -> ActiveVoiceAgent:
        try:
            return self._active[profile_id]
        except KeyError as exc:
            raise VoiceAgentNotActiveError(
                f"语音智能体尚未发布: {profile_id}"
            ) from exc

    def current_or_none(self, profile_id: str) -> ActiveVoiceAgent | None:
        return self._active.get(profile_id)

    async def list(self) -> tuple[VoiceAgentSummary, ...]:
        return await self.repository.list()

    async def create(
        self,
        *,
        slug: str,
        name: str,
        description: str | None,
        definition: VoiceAgentDefinition,
    ) -> VoiceAgentSummary:
        return await self.repository.create(
            slug=slug,
            name=name,
            description=description,
            definition=definition,
        )

    async def get_summary(self, profile_id: str) -> VoiceAgentSummary:
        return await self.repository.get_summary(profile_id)

    async def update_metadata(
        self,
        profile_id: str,
        *,
        name: str | None,
        description: str | None,
    ) -> VoiceAgentSummary:
        return await self.repository.update_metadata(
            profile_id,
            name=name,
            description=description,
        )

    async def get_draft(self, profile_id: str) -> VoiceAgentDraft:
        return await self.repository.get_draft(profile_id)

    async def update_draft(
        self,
        profile_id: str,
        *,
        definition: VoiceAgentDefinition,
        expected_revision: int,
    ) -> VoiceAgentDraft:
        return await self.repository.update_draft(
            profile_id,
            definition=definition,
            expected_revision=expected_revision,
        )

    async def _inspect_definition(
        self,
        definition: VoiceAgentDefinition,
    ) -> tuple[tuple[VoiceAgentDiagnostic, ...], _VoiceAgentInputs | None]:
        diagnostics: list[VoiceAgentDiagnostic] = []
        audio = self.audio_workflows.current_or_none(definition.audio_workflow_id)
        if audio is None:
            diagnostics.append(
                VoiceAgentDiagnostic(
                    code="audio_workflow_not_active",
                    message="绑定的语音工作流尚未发布",
                    field="audio_workflow_id",
                )
            )
        assets = await self.repository.engine_assets()
        responses_asset = (
            assets.get(definition.responses_engine_id)
            if definition.responses_engine_id is not None
            else None
        )
        if responses_asset is None:
            diagnostics.append(
                VoiceAgentDiagnostic(
                    code="responses_engine_missing",
                    message="尚未绑定有效的 Responses 引擎",
                    field="responses_engine_id",
                )
            )
        elif responses_asset.api is not EngineApi.RESPONSES:
            diagnostics.append(
                VoiceAgentDiagnostic(
                    code="responses_engine_api_mismatch",
                    message="Responses 引擎 API 不匹配",
                    field="responses_engine_id",
                )
            )
        elif not responses_asset.enabled:
            diagnostics.append(
                VoiceAgentDiagnostic(
                    code="responses_engine_disabled",
                    message="Responses 引擎已停用",
                    field="responses_engine_id",
                )
            )

        tts_asset = (
            assets.get(definition.tts_engine_id)
            if definition.tts_engine_id is not None
            else None
        )
        if tts_asset is None:
            diagnostics.append(
                VoiceAgentDiagnostic(
                    code="tts_engine_missing",
                    message="尚未绑定有效的 TTS 引擎",
                    field="tts_engine_id",
                )
            )
        elif tts_asset.api is not EngineApi.QWEN_TTS_DUPLEX:
            diagnostics.append(
                VoiceAgentDiagnostic(
                    code="tts_engine_api_mismatch",
                    message="TTS 引擎 API 不匹配",
                    field="tts_engine_id",
                )
            )
        elif not tts_asset.enabled:
            diagnostics.append(
                VoiceAgentDiagnostic(
                    code="tts_engine_disabled",
                    message="TTS 引擎已停用",
                    field="tts_engine_id",
                )
            )
        if diagnostics or audio is None or responses_asset is None or tts_asset is None:
            return tuple(diagnostics), None
        return (
            (),
            _VoiceAgentInputs(
                audio=audio,
                responses=responses_asset.snapshot(),
                tts=tts_asset.snapshot(),
            ),
        )

    async def validate_draft(
        self,
        profile_id: str,
    ) -> tuple[VoiceAgentDiagnostic, ...]:
        draft = await self.get_draft(profile_id)
        diagnostics, _inputs = await self._inspect_definition(draft.definition)
        return diagnostics

    async def publish(
        self,
        profile_id: str,
        *,
        expected_revision: int,
    ) -> ActiveVoiceAgent:
        async with self._publish_lock:
            draft = await self.get_draft(profile_id)
            if draft.revision != expected_revision:
                raise VoiceAgentConflictError("语音智能体草稿已经被其他操作更新")
            definition = draft.definition
            diagnostics, inputs = await self._inspect_definition(definition)
            if inputs is None:
                raise VoiceAgentPublishError(
                    "；".join(item.message for item in diagnostics)
                )
            audio = inputs.audio
            responses = inputs.responses
            tts = inputs.tts
            try:
                await self._inferencer_pool.prepare_all((responses, tts))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise VoiceAgentPublishError(f"语音智能体引擎准备失败: {exc}") from exc
            compiled = {
                "schema_version": 1,
                "audio_workflow_revision_id": audio.database_revision_id,
                "responses_engine": _snapshot_document(responses),
                "tts_engine": _snapshot_document(tts),
            }
            stored = await self.repository.activate(
                profile_id,
                expected_revision=expected_revision,
                definition=definition,
                compiled_profile=compiled,
                audio_workflow_revision_id=audio.database_revision_id,
                engine_snapshots=(responses, tts),
            )
            active = ActiveVoiceAgent(
                id=profile_id,
                revision=stored.revision,
                definition=definition,
                audio_workflow=audio,
                responses_engine=responses,
                tts_engine=tts,
            )
            previous = self._active.get(profile_id)
            self._active[profile_id] = active
            if previous is not None:
                live = {
                    (snapshot.id, snapshot.revision)
                    for profile in self._active.values()
                    for snapshot in profile.engine_snapshots
                }
                for snapshot in previous.engine_snapshots:
                    if (snapshot.id, snapshot.revision) not in live:
                        await self._inferencer_pool.retire_snapshot(
                            snapshot.id, snapshot.revision
                        )
            return active

    async def get_revision(
        self,
        profile_id: str,
        revision: int,
    ) -> ActiveVoiceAgent:
        return await self._restore(
            await self.repository.get_revision(profile_id, revision)
        )

    async def list_revisions(
        self,
        profile_id: str,
    ) -> tuple[ActiveVoiceAgent, ...]:
        result: list[ActiveVoiceAgent] = []
        for stored in await self.repository.list_revisions(profile_id):
            result.append(await self._restore(stored))
        return tuple(result)

    async def restore_as_draft(
        self,
        profile_id: str,
        revision: int,
        *,
        expected_revision: int,
    ) -> VoiceAgentDraft:
        return await self.repository.restore_as_draft(
            profile_id,
            revision,
            expected_revision=expected_revision,
        )

    async def delete(self, profile_id: str) -> None:
        await self.repository.delete(profile_id)
        previous = self._active.pop(profile_id, None)
        if previous is None:
            return
        live = {
            (snapshot.id, snapshot.revision)
            for profile in self._active.values()
            for snapshot in profile.engine_snapshots
        }
        for snapshot in previous.engine_snapshots:
            if (snapshot.id, snapshot.revision) not in live:
                await self._inferencer_pool.retire_snapshot(
                    snapshot.id,
                    snapshot.revision,
                )

    async def _restore(self, stored: StoredVoiceAgentRevision) -> ActiveVoiceAgent:
        document = stored.compiled_profile
        if document.get("schema_version") != 1:
            raise ValueError("语音智能体执行计划版本不支持")
        responses = _restore_snapshot(document.get("responses_engine"))
        tts = _restore_snapshot(document.get("tts_engine"))
        if responses.api is not EngineApi.RESPONSES:
            raise ValueError("语音智能体 Responses 快照 API 不匹配")
        if tts.api is not EngineApi.QWEN_TTS_DUPLEX:
            raise ValueError("语音智能体 TTS 快照 API 不匹配")
        return ActiveVoiceAgent(
            id=stored.voice_agent_id,
            revision=stored.revision,
            definition=stored.definition,
            audio_workflow=await self.audio_workflows.get_revision_by_id(
                stored.audio_workflow_revision_id
            ),
            responses_engine=responses,
            tts_engine=tts,
        )


RealtimeTarget = ActiveAudioWorkflow | ActiveVoiceAgent


@dataclass(frozen=True)
class RealtimeRouteSnapshot:
    model: str
    target_type: RealtimeRouteTargetType
    target_id: str
    visibility: ModelRouteVisibility
    updated_at: datetime


class RealtimeRouteService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        audio_workflows: AudioWorkflowService,
        voice_agents: VoiceAgentService,
    ) -> None:
        self._session_factory = session_factory
        self._audio_workflows = audio_workflows
        self._voice_agents = voice_agents
        self._routes: dict[str, RealtimeRouteSnapshot] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._session_factory() as session:
            models = (await session.execute(select(RealtimeRoute))).scalars().all()
        self._routes = {
            model.model: RealtimeRouteSnapshot(
                model=model.model,
                target_type=model.target_type,
                target_id=model.target_id,
                visibility=model.visibility,
                updated_at=model.updated_at,
            )
            for model in models
        }

    def list(self) -> tuple[RealtimeRouteSnapshot, ...]:
        return tuple(self._routes[key] for key in sorted(self._routes))

    def get(self, model: str) -> RealtimeRouteSnapshot:
        try:
            return self._routes[model]
        except KeyError as exc:
            raise RealtimeRouteNotFoundError(
                f"Realtime 模型路由不存在: {model}"
            ) from exc

    def resolve(self, model: str, *, allow_internal: bool = False) -> RealtimeTarget:
        route = self._routes.get(model)
        if route is None or (
            route.visibility is ModelRouteVisibility.INTERNAL and not allow_internal
        ):
            raise RealtimeRouteNotFoundError(f"Realtime 模型路由不存在: {model}")
        if route.target_type is RealtimeRouteTargetType.AUDIO_WORKFLOW:
            return self._audio_workflows.current(route.target_id)
        return self._voice_agents.current(route.target_id)

    async def upsert(
        self,
        *,
        model: str,
        target_type: RealtimeRouteTargetType,
        target_id: str,
        visibility: ModelRouteVisibility,
        replace_existing: bool = False,
    ) -> RealtimeRouteSnapshot:
        if target_type is RealtimeRouteTargetType.AUDIO_WORKFLOW:
            self._audio_workflows.current(target_id)
        else:
            self._voice_agents.current(target_id)
        now = utc_now()
        async with self._lock:
            async with self._session_factory() as session:
                route = await session.get(RealtimeRoute, model)
                if route is None:
                    route = RealtimeRoute(
                        model=model,
                        target_type=target_type,
                        target_id=target_id,
                        visibility=visibility,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(route)
                else:
                    current = RealtimeRouteSnapshot(
                        model=route.model,
                        target_type=route.target_type,
                        target_id=route.target_id,
                        visibility=route.visibility,
                        updated_at=route.updated_at,
                    )
                    target_changed = (
                        route.target_type != target_type
                        or route.target_id != target_id
                    )
                    if target_changed and not replace_existing:
                        raise RealtimeRouteConflictError(
                            f"Realtime 模型 {model} 已指向其他目标",
                            current=current,
                        )
                    route.target_type = target_type
                    route.target_id = target_id
                    route.visibility = visibility
                    route.updated_at = now
                await session.commit()
            snapshot = RealtimeRouteSnapshot(
                model=model,
                target_type=target_type,
                target_id=target_id,
                visibility=visibility,
                updated_at=now,
            )
            self._routes[model] = snapshot
            return snapshot

    async def delete(self, model: str) -> None:
        async with self._lock:
            async with self._session_factory() as session:
                route = await session.get(RealtimeRoute, model)
                if route is None:
                    raise RealtimeRouteNotFoundError(
                        f"Realtime 模型路由不存在: {model}"
                    )
                await session.delete(route)
                await session.commit()
            self._routes.pop(model, None)
