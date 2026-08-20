from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from audio_harness.agent_backends.catalog import (
    AgentBackendCatalog,
    AgentBackendCatalogError,
)
from audio_harness.agent_backends.models import (
    AgentBackendKind,
    AgentBackendProtocol,
    AgentBackendSnapshot,
)
from audio_harness.engines.models import EngineApi, EngineProtocol, EngineSnapshot
from audio_harness.engines.pool import InferencerPool
from audio_harness.voice_agents.models import VoiceAgentDefinition
from audio_harness.voice_agents.repository import (
    StoredVoiceAgentRevision,
    VoiceAgentConflictError,
    VoiceAgentDraft,
    VoiceAgentRepository,
    VoiceAgentSummary,
)
from audio_harness.workflows.service import ActiveAudioWorkflow, AudioWorkflowService


class VoiceAgentNotActiveError(RuntimeError):
    pass


class VoiceAgentPublishError(RuntimeError):
    pass


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
    agent_backend: AgentBackendSnapshot | None


@dataclass(frozen=True)
class ActiveVoiceAgent:
    id: str
    revision: int
    definition: VoiceAgentDefinition
    audio_workflow: ActiveAudioWorkflow
    responses_engine: EngineSnapshot
    tts_engine: EngineSnapshot
    agent_backend: AgentBackendSnapshot | None = None

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


def _agent_backend_snapshot_document(
    snapshot: AgentBackendSnapshot,
) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "revision": snapshot.revision,
        "name": snapshot.name,
        "kind": snapshot.kind.value,
        "protocol": snapshot.protocol.value,
        "target": snapshot.target,
        "connect_timeout_seconds": snapshot.connect_timeout_seconds,
        "request_timeout_seconds": snapshot.request_timeout_seconds,
        "headers_env": dict(snapshot.headers_env),
        "details": dict(snapshot.details),
    }


def _restore_agent_backend_snapshot(raw: object) -> AgentBackendSnapshot:
    if not isinstance(raw, dict):
        raise ValueError("语音智能体执行计划缺少 Agent Backend 快照")
    return AgentBackendSnapshot(
        id=str(raw["id"]),
        revision=int(raw["revision"]),
        name=str(raw["name"]),
        kind=AgentBackendKind(str(raw["kind"])),
        protocol=AgentBackendProtocol(str(raw["protocol"])),
        target=str(raw["target"]),
        connect_timeout_seconds=float(raw["connect_timeout_seconds"]),
        request_timeout_seconds=float(raw["request_timeout_seconds"]),
        headers_env=dict(raw.get("headers_env") or {}),
        details=dict(raw.get("details") or {}),
    )


class VoiceAgentService:
    def __init__(
        self,
        repository: VoiceAgentRepository,
        *,
        audio_workflows: AudioWorkflowService,
        inferencer_pool: InferencerPool,
        agent_backends: AgentBackendCatalog | None = None,
    ) -> None:
        self.repository = repository
        self.audio_workflows = audio_workflows
        self._inferencer_pool = inferencer_pool
        self._agent_backends = agent_backends
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
        backend_snapshot: AgentBackendSnapshot | None = None
        binding = definition.agent_backend
        if binding is not None:
            if self._agent_backends is None:
                diagnostics.append(
                    VoiceAgentDiagnostic(
                        code="agent_backend_unavailable",
                        message="Agent Backend 控制面未配置",
                        field="agent_backend.backend_id",
                    )
                )
            else:
                try:
                    backend_asset = await self._agent_backends.get_asset(
                        binding.backend_id
                    )
                    if not backend_asset.enabled:
                        diagnostics.append(
                            VoiceAgentDiagnostic(
                                code="agent_backend_disabled",
                                message="Agent Backend 已停用",
                                field="agent_backend.backend_id",
                            )
                        )
                        backend_asset = None
                    if backend_asset is not None:
                        backend_asset, capabilities = (
                            await self._agent_backends.capabilities(
                                binding.backend_id
                            )
                        )
                except AgentBackendCatalogError as exc:
                    diagnostics.append(
                        VoiceAgentDiagnostic(
                            code=getattr(
                                exc,
                                "code",
                                "agent_backend_unavailable",
                            ),
                            message=str(exc),
                            field="agent_backend.backend_id",
                        )
                    )
                else:
                    if backend_asset is None:
                        pass
                    elif binding.agent_preset not in capabilities.presets:
                        diagnostics.append(
                            VoiceAgentDiagnostic(
                                code="agent_backend_preset_missing",
                                message=(
                                    "Agent Backend 不存在 preset: "
                                    f"{binding.agent_preset}"
                                ),
                                field="agent_backend.agent_preset",
                            )
                        )
                    else:
                        backend_snapshot = backend_asset.snapshot()
        if diagnostics or audio is None or responses_asset is None or tts_asset is None:
            return tuple(diagnostics), None
        return (
            (),
            _VoiceAgentInputs(
                audio=audio,
                responses=responses_asset.snapshot(),
                tts=tts_asset.snapshot(),
                agent_backend=backend_snapshot,
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
            agent_backend = inputs.agent_backend
            try:
                await self._inferencer_pool.prepare_all((responses, tts))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise VoiceAgentPublishError(f"语音智能体引擎准备失败: {exc}") from exc
            compiled = {
                "schema_version": 2,
                "audio_workflow_revision_id": audio.database_revision_id,
                "responses_engine": _snapshot_document(responses),
                "tts_engine": _snapshot_document(tts),
                "agent_backend": (
                    _agent_backend_snapshot_document(agent_backend)
                    if agent_backend is not None
                    else None
                ),
            }
            stored = await self.repository.activate(
                profile_id,
                expected_revision=expected_revision,
                definition=definition,
                compiled_profile=compiled,
                audio_workflow_revision_id=audio.database_revision_id,
                engine_snapshots=(responses, tts),
                agent_backend_snapshot=agent_backend,
            )
            active = ActiveVoiceAgent(
                id=profile_id,
                revision=stored.revision,
                definition=definition,
                audio_workflow=audio,
                responses_engine=responses,
                tts_engine=tts,
                agent_backend=agent_backend,
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
                if (
                    self._agent_backends is not None
                    and previous.agent_backend is not None
                ):
                    live_backends = {
                        (profile.agent_backend.id, profile.agent_backend.revision)
                        for profile in self._active.values()
                        if profile.agent_backend is not None
                    }
                    previous_backend = previous.agent_backend
                    if (
                        previous_backend.id,
                        previous_backend.revision,
                    ) not in live_backends:
                        await self._agent_backends.retire_snapshot(
                            previous_backend.id,
                            previous_backend.revision,
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
        if self._agent_backends is not None and previous.agent_backend is not None:
            live_backends = {
                (profile.agent_backend.id, profile.agent_backend.revision)
                for profile in self._active.values()
                if profile.agent_backend is not None
            }
            backend = previous.agent_backend
            if (backend.id, backend.revision) not in live_backends:
                await self._agent_backends.retire_snapshot(
                    backend.id,
                    backend.revision,
                )

    async def _restore(self, stored: StoredVoiceAgentRevision) -> ActiveVoiceAgent:
        document = stored.compiled_profile
        schema_version = document.get("schema_version")
        if schema_version not in {1, 2}:
            raise ValueError("语音智能体执行计划版本不支持")
        responses = _restore_snapshot(document.get("responses_engine"))
        tts = _restore_snapshot(document.get("tts_engine"))
        if responses.api is not EngineApi.RESPONSES:
            raise ValueError("语音智能体 Responses 快照 API 不匹配")
        if tts.api is not EngineApi.QWEN_TTS_DUPLEX:
            raise ValueError("语音智能体 TTS 快照 API 不匹配")
        agent_backend = (
            _restore_agent_backend_snapshot(document.get("agent_backend"))
            if schema_version == 2 and document.get("agent_backend") is not None
            else None
        )
        return ActiveVoiceAgent(
            id=stored.voice_agent_id,
            revision=stored.revision,
            definition=stored.definition,
            audio_workflow=await self.audio_workflows.get_revision_by_id(
                stored.audio_workflow_revision_id
            ),
            responses_engine=responses,
            tts_engine=tts,
            agent_backend=agent_backend,
        )
