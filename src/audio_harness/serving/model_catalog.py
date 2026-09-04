from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from audio_harness.voice_agents.service import ActiveVoiceAgent, VoiceAgentService
from audio_harness.workflows.service import ActiveAudioWorkflow, AudioWorkflowService


class RealtimeModelNotFoundError(RuntimeError):
    pass


RealtimeTarget = ActiveAudioWorkflow | ActiveVoiceAgent


@dataclass(frozen=True)
class RealtimeModel:
    id: str
    created_at: datetime

    def openai_document(self) -> dict[str, object]:
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return {
            "id": self.id,
            "object": "model",
            "created": int(created.timestamp()),
            "owned_by": "audio-harness",
        }


class RealtimeModelCatalog:
    """将已发布资源直接作为 Realtime model 暴露。"""

    def __init__(
        self,
        *,
        audio_workflows: AudioWorkflowService,
        voice_agents: VoiceAgentService,
    ) -> None:
        self._audio_workflows = audio_workflows
        self._voice_agents = voice_agents

    async def list_models(self) -> tuple[RealtimeModel, ...]:
        workflows, agents = await self._published_resources()
        return tuple(sorted((*workflows, *agents), key=lambda item: item.id))

    async def resolve(self, model: str) -> RealtimeTarget:
        workflows, agents = await self._published_resources()
        for item in (*workflows, *agents):
            if item.id != model:
                continue
            if isinstance(item, _WorkflowModel):
                return self._audio_workflows.current(item.resource_id)
            return self._voice_agents.current(item.resource_id)
        raise RealtimeModelNotFoundError(f"Realtime 模型不存在: {model}")

    async def slug_available(self, slug: str) -> bool:
        workflows = await self._audio_workflows.list()
        agents = await self._voice_agents.list()
        return all(item.slug != slug for item in (*workflows, *agents))

    async def _published_resources(
        self,
    ) -> tuple[tuple[_WorkflowModel, ...], tuple[_VoiceAgentModel, ...]]:
        workflows = await self._audio_workflows.list()
        agents = await self._voice_agents.list()
        active_workflows = {
            item.id for item in self._audio_workflows.active_workflows()
        }
        active_agents = set(self._voice_agents.active_agents())
        return (
            tuple(
                _WorkflowModel(item.slug, item.created_at, item.id)
                for item in workflows
                if item.id in active_workflows
            ),
            tuple(
                _VoiceAgentModel(item.slug, item.created_at, item.id)
                for item in agents
                if item.id in active_agents
            ),
        )


@dataclass(frozen=True)
class _WorkflowModel(RealtimeModel):
    resource_id: str


@dataclass(frozen=True)
class _VoiceAgentModel(RealtimeModel):
    resource_id: str
