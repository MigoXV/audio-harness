from __future__ import annotations

import asyncio
from dataclasses import dataclass

from audio_harness.engines.pool import InferencerPool
from audio_harness.workflows.compiler import (
    CompiledAudioGraph,
    compile_audio_graph,
    compiled_audio_graph_document,
    restore_audio_graph,
)
from audio_harness.workflows.models import AudioWorkflowDocument
from audio_harness.workflows.repository import (
    AudioWorkflowDraft,
    AudioWorkflowRepository,
    AudioWorkflowSummary,
    StoredAudioWorkflowRevision,
)


class AudioWorkflowNotActiveError(RuntimeError):
    pass


class AudioWorkflowPublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActiveAudioWorkflow:
    id: str
    database_revision_id: int
    revision: int
    document: AudioWorkflowDocument
    plan: CompiledAudioGraph


class ActiveAudioWorkflowRegistry:
    def __init__(self) -> None:
        self._active: dict[str, ActiveAudioWorkflow] = {}

    def replace_all(self, values: tuple[ActiveAudioWorkflow, ...]) -> None:
        self._active = {value.id: value for value in values}

    def set(self, value: ActiveAudioWorkflow) -> ActiveAudioWorkflow | None:
        previous = self._active.get(value.id)
        self._active[value.id] = value
        return previous

    def remove(self, workflow_id: str) -> ActiveAudioWorkflow | None:
        return self._active.pop(workflow_id, None)

    def get(self, workflow_id: str) -> ActiveAudioWorkflow:
        try:
            return self._active[workflow_id]
        except KeyError as exc:
            raise AudioWorkflowNotActiveError(
                f"语音工作流尚未发布: {workflow_id}"
            ) from exc

    def get_or_none(self, workflow_id: str) -> ActiveAudioWorkflow | None:
        return self._active.get(workflow_id)

    def all(self) -> tuple[ActiveAudioWorkflow, ...]:
        return tuple(self._active[key] for key in sorted(self._active))


class AudioWorkflowService:
    """语音图编译、发布和活动快照服务。"""

    def __init__(
        self,
        repository: AudioWorkflowRepository,
        *,
        inferencer_pool: InferencerPool,
    ) -> None:
        self.repository = repository
        self._inferencer_pool = inferencer_pool
        self._registry = ActiveAudioWorkflowRegistry()
        self._publish_lock = asyncio.Lock()

    async def initialize(self) -> None:
        active = tuple(
            self._active_from_stored(item)
            for item in await self.repository.active_revisions()
        )
        self._registry.replace_all(active)

    def current(self, workflow_id: str) -> ActiveAudioWorkflow:
        return self._registry.get(workflow_id)

    def current_or_none(self, workflow_id: str) -> ActiveAudioWorkflow | None:
        return self._registry.get_or_none(workflow_id)

    def active_workflows(self) -> tuple[ActiveAudioWorkflow, ...]:
        return self._registry.all()

    async def list(self) -> tuple[AudioWorkflowSummary, ...]:
        return await self.repository.list()

    async def create(
        self,
        *,
        slug: str,
        name: str,
        description: str | None,
        document: AudioWorkflowDocument | None = None,
    ) -> AudioWorkflowSummary:
        return await self.repository.create(
            slug=slug,
            name=name,
            description=description,
            document=document,
        )

    async def get_summary(self, workflow_id: str) -> AudioWorkflowSummary:
        return await self.repository.get_summary(workflow_id)

    async def update_metadata(
        self,
        workflow_id: str,
        *,
        name: str | None,
        description: str | None,
    ) -> AudioWorkflowSummary:
        return await self.repository.update_metadata(
            workflow_id,
            name=name,
            description=description,
        )

    async def get_draft(self, workflow_id: str) -> AudioWorkflowDraft:
        return await self.repository.get_draft(workflow_id)

    async def update_draft(
        self,
        workflow_id: str,
        *,
        document: AudioWorkflowDocument,
        expected_revision: int,
    ) -> AudioWorkflowDraft:
        return await self.repository.update_draft(
            workflow_id,
            document=document,
            expected_revision=expected_revision,
        )

    async def compile(self, document: AudioWorkflowDocument) -> CompiledAudioGraph:
        return compile_audio_graph(
            document,
            assets=await self.repository.engine_assets(),
        )

    async def validate_draft(self, workflow_id: str) -> CompiledAudioGraph:
        return await self.compile((await self.get_draft(workflow_id)).document)

    async def publish(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
    ) -> ActiveAudioWorkflow:
        async with self._publish_lock:
            draft = await self.get_draft(workflow_id)
            if draft.revision != expected_revision:
                from audio_harness.workflows.repository import AudioWorkflowConflictError

                raise AudioWorkflowConflictError("语音工作流草稿已经被其他操作更新")
            plan = await self.compile(draft.document)
            try:
                await self._inferencer_pool.prepare_all(plan.engine_snapshots)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise AudioWorkflowPublishError(f"语音工作流引擎准备失败: {exc}") from exc
            stored = await self.repository.activate(
                workflow_id,
                expected_revision=expected_revision,
                document=draft.document,
                compiled_plan=compiled_audio_graph_document(plan),
                engine_snapshots=plan.engine_snapshots,
            )
            active = ActiveAudioWorkflow(
                id=workflow_id,
                database_revision_id=stored.id,
                revision=stored.revision,
                document=stored.document,
                plan=plan,
            )
            previous = self._registry.set(active)
            if previous is not None:
                live_keys = {
                    (snapshot.id, snapshot.revision)
                    for workflow in self._registry.all()
                    for snapshot in workflow.plan.engine_snapshots
                }
                for snapshot in previous.plan.engine_snapshots:
                    if (snapshot.id, snapshot.revision) not in live_keys:
                        await self._inferencer_pool.retire_snapshot(
                            snapshot.id, snapshot.revision
                        )
            return active

    async def get_revision(
        self,
        workflow_id: str,
        revision: int,
    ) -> ActiveAudioWorkflow:
        return self._active_from_stored(
            await self.repository.get_revision(workflow_id, revision)
        )

    async def get_revision_by_id(self, revision_id: int) -> ActiveAudioWorkflow:
        return self._active_from_stored(
            await self.repository.get_revision_by_id(revision_id)
        )

    async def list_revisions(
        self,
        workflow_id: str,
    ) -> tuple[ActiveAudioWorkflow, ...]:
        return tuple(
            self._active_from_stored(item)
            for item in await self.repository.list_revisions(workflow_id)
        )

    async def restore_as_draft(
        self,
        workflow_id: str,
        revision: int,
        *,
        expected_revision: int,
    ) -> AudioWorkflowDraft:
        return await self.repository.restore_as_draft(
            workflow_id,
            revision,
            expected_revision=expected_revision,
        )

    async def delete(self, workflow_id: str) -> None:
        await self.repository.delete(workflow_id)
        previous = self._registry.remove(workflow_id)
        if previous is not None:
            for snapshot in previous.plan.engine_snapshots:
                await self._inferencer_pool.retire_snapshot(snapshot.id, snapshot.revision)

    @staticmethod
    def _active_from_stored(stored: StoredAudioWorkflowRevision) -> ActiveAudioWorkflow:
        return ActiveAudioWorkflow(
            id=stored.audio_workflow_id,
            database_revision_id=stored.id,
            revision=stored.revision,
            document=stored.document,
            plan=restore_audio_graph(stored.document, stored.compiled_plan),
        )
