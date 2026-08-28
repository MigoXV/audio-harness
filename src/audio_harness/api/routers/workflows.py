from __future__ import annotations

from fastapi import APIRouter, Response, status

from audio_harness.schemas.voice_agents import (
    ConfigurationDiagnosticRead,
    VoiceAgentCreate,
    VoiceAgentDraftRead,
    VoiceAgentDraftUpdate,
    VoiceAgentPatch,
    VoiceAgentRead,
    VoiceAgentRevisionRead,
    VoiceAgentValidationRead,
)
from audio_harness.schemas.workflows import (
    AudioNodeTypeRead,
    AudioWorkflowCreate,
    AudioWorkflowDraftRead,
    AudioWorkflowDraftUpdate,
    AudioWorkflowPatch,
    AudioWorkflowRead,
    AudioWorkflowRevisionRead,
    AudioWorkflowValidationRead,
    GraphDiagnosticRead,
    NodeInputGroupRead,
    NodePortRead,
    PublishRequest,
    RestoreRequest,
)
from audio_harness.voice_agents.service import VoiceAgentService
from audio_harness.workflows.compiler import (
    AudioGraphValidationError,
    audio_node_catalog,
)
from audio_harness.workflows.repository import AudioWorkflowDraft, AudioWorkflowSummary
from audio_harness.workflows.service import ActiveAudioWorkflow, AudioWorkflowService


def workflow_summary(value: AudioWorkflowSummary) -> AudioWorkflowRead:
    return AudioWorkflowRead(**value.__dict__)


def workflow_draft(value: AudioWorkflowDraft) -> AudioWorkflowDraftRead:
    return AudioWorkflowDraftRead(**value.__dict__)


def workflow_revision(value: ActiveAudioWorkflow) -> AudioWorkflowRevisionRead:
    return AudioWorkflowRevisionRead(
        workflow_id=value.id,
        revision=value.revision,
        document=value.document,
        engine_revisions={
            node_id: node.engine_snapshot.revision
            for node_id, node in value.plan.nodes.items()
            if node.engine_snapshot is not None
        },
    )


def voice_agent_revision(value) -> VoiceAgentRevisionRead:
    return VoiceAgentRevisionRead(
        id=value.id,
        revision=value.revision,
        definition=value.definition,
        audio_workflow_id=value.audio_workflow.id,
        audio_workflow_revision=value.audio_workflow.revision,
        engine_revisions={
            "responses": value.responses_engine.revision,
            "tts": value.tts_engine.revision,
        },
        agent_backend_revision=(
            value.agent_backend.revision
            if value.agent_backend is not None
            else None
        ),
    )


def create_workflow_router(
    workflows: AudioWorkflowService,
    voice_agents: VoiceAgentService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["workflows"])

    @router.get("/audio-node-types", response_model=list[AudioNodeTypeRead])
    async def list_node_types() -> list[AudioNodeTypeRead]:
        return [
            AudioNodeTypeRead(
                kind=spec.kind,
                display_name=spec.display_name,
                required_api=spec.required_api,
                required_capability=spec.required_capability,
                inputs=[
                    NodePortRead(
                        name=name,
                        kind=port.kind.value,
                        accepted_kinds=[
                            kind.value for kind in port.accepted_kinds
                        ],
                    )
                    for name, port in spec.inputs.items()
                ],
                outputs=[
                    NodePortRead(name=name, kind=port.kind.value)
                    for name, port in spec.outputs.items()
                ],
                input_groups=[
                    NodeInputGroupRead(
                        name=group.name,
                        ports=list(group.ports),
                        minimum=group.minimum,
                        maximum=group.maximum,
                    )
                    for group in spec.input_groups
                ],
                config_schema=spec.config_model.model_json_schema(),
            )
            for spec in audio_node_catalog()
        ]

    @router.get("/audio-workflows", response_model=list[AudioWorkflowRead])
    async def list_audio_workflows() -> list[AudioWorkflowRead]:
        return [workflow_summary(item) for item in await workflows.list()]

    @router.post(
        "/audio-workflows",
        response_model=AudioWorkflowRead,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_audio_workflow(payload: AudioWorkflowCreate) -> AudioWorkflowRead:
        return workflow_summary(
            await workflows.create(
                slug=payload.slug,
                name=payload.name,
                description=payload.description,
                document=payload.document,
            )
        )

    @router.get("/audio-workflows/{workflow_id}", response_model=AudioWorkflowRead)
    async def get_audio_workflow(workflow_id: str) -> AudioWorkflowRead:
        return workflow_summary(await workflows.get_summary(workflow_id))

    @router.patch("/audio-workflows/{workflow_id}", response_model=AudioWorkflowRead)
    async def patch_audio_workflow(
        workflow_id: str,
        payload: AudioWorkflowPatch,
    ) -> AudioWorkflowRead:
        return workflow_summary(
            await workflows.update_metadata(
                workflow_id,
                name=payload.name,
                description=payload.description,
            )
        )

    @router.delete(
        "/audio-workflows/{workflow_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_audio_workflow(workflow_id: str) -> Response:
        await workflows.delete(workflow_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/audio-workflows/{workflow_id}/draft",
        response_model=AudioWorkflowDraftRead,
    )
    async def get_audio_workflow_draft(workflow_id: str) -> AudioWorkflowDraftRead:
        return workflow_draft(await workflows.get_draft(workflow_id))

    @router.put(
        "/audio-workflows/{workflow_id}/draft",
        response_model=AudioWorkflowDraftRead,
    )
    async def update_audio_workflow_draft(
        workflow_id: str,
        payload: AudioWorkflowDraftUpdate,
    ) -> AudioWorkflowDraftRead:
        return workflow_draft(
            await workflows.update_draft(
                workflow_id,
                document=payload.document,
                expected_revision=payload.expected_revision,
            )
        )

    @router.post(
        "/audio-workflows/{workflow_id}/validate",
        response_model=AudioWorkflowValidationRead,
    )
    async def validate_audio_workflow(workflow_id: str) -> AudioWorkflowValidationRead:
        try:
            plan = await workflows.validate_draft(workflow_id)
        except AudioGraphValidationError as exc:
            return AudioWorkflowValidationRead(
                valid=False,
                diagnostics=[GraphDiagnosticRead(**item.__dict__) for item in exc.diagnostics],
            )
        return AudioWorkflowValidationRead(
            valid=True,
            diagnostics=[],
            topological_order=list(plan.topological_order),
        )

    @router.post(
        "/audio-workflows/{workflow_id}/publish",
        response_model=AudioWorkflowRevisionRead,
    )
    async def publish_audio_workflow(
        workflow_id: str,
        payload: PublishRequest,
    ) -> AudioWorkflowRevisionRead:
        return workflow_revision(
            await workflows.publish(
                workflow_id,
                expected_revision=payload.expected_revision,
            )
        )

    @router.get(
        "/audio-workflows/{workflow_id}/active",
        response_model=AudioWorkflowRevisionRead,
    )
    async def active_audio_workflow(workflow_id: str) -> AudioWorkflowRevisionRead:
        return workflow_revision(workflows.current(workflow_id))

    @router.get(
        "/audio-workflows/{workflow_id}/revisions",
        response_model=list[AudioWorkflowRevisionRead],
    )
    async def list_audio_workflow_revisions(
        workflow_id: str,
    ) -> list[AudioWorkflowRevisionRead]:
        return [
            workflow_revision(item)
            for item in await workflows.list_revisions(workflow_id)
        ]

    @router.post(
        "/audio-workflows/{workflow_id}/revisions/{revision}/restore",
        response_model=AudioWorkflowDraftRead,
    )
    async def restore_audio_workflow_revision(
        workflow_id: str,
        revision: int,
        payload: RestoreRequest,
    ) -> AudioWorkflowDraftRead:
        return workflow_draft(
            await workflows.restore_as_draft(
                workflow_id,
                revision,
                expected_revision=payload.expected_revision,
            )
        )

    @router.get("/voice-agents", response_model=list[VoiceAgentRead])
    async def list_voice_agents() -> list[VoiceAgentRead]:
        return [VoiceAgentRead(**item.__dict__) for item in await voice_agents.list()]

    @router.post(
        "/voice-agents",
        response_model=VoiceAgentRead,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_voice_agent(payload: VoiceAgentCreate) -> VoiceAgentRead:
        value = await voice_agents.create(
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
            definition=payload.definition,
        )
        return VoiceAgentRead(**value.__dict__)

    @router.get("/voice-agents/{profile_id}", response_model=VoiceAgentRead)
    async def get_voice_agent(profile_id: str) -> VoiceAgentRead:
        return VoiceAgentRead(**(await voice_agents.get_summary(profile_id)).__dict__)

    @router.patch("/voice-agents/{profile_id}", response_model=VoiceAgentRead)
    async def patch_voice_agent(
        profile_id: str,
        payload: VoiceAgentPatch,
    ) -> VoiceAgentRead:
        value = await voice_agents.update_metadata(
            profile_id,
            name=payload.name,
            description=payload.description,
        )
        return VoiceAgentRead(**value.__dict__)

    @router.delete(
        "/voice-agents/{profile_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_voice_agent(profile_id: str) -> Response:
        await voice_agents.delete(profile_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/voice-agents/{profile_id}/draft", response_model=VoiceAgentDraftRead)
    async def get_voice_agent_draft(profile_id: str) -> VoiceAgentDraftRead:
        return VoiceAgentDraftRead(**(await voice_agents.get_draft(profile_id)).__dict__)

    @router.put("/voice-agents/{profile_id}/draft", response_model=VoiceAgentDraftRead)
    async def update_voice_agent_draft(
        profile_id: str,
        payload: VoiceAgentDraftUpdate,
    ) -> VoiceAgentDraftRead:
        value = await voice_agents.update_draft(
            profile_id,
            definition=payload.definition,
            expected_revision=payload.expected_revision,
        )
        return VoiceAgentDraftRead(**value.__dict__)

    @router.post(
        "/voice-agents/{profile_id}/validate",
        response_model=VoiceAgentValidationRead,
        response_model_exclude_none=True,
    )
    async def validate_voice_agent(profile_id: str) -> VoiceAgentValidationRead:
        diagnostics, context_preview, speaker_context_preview = await voice_agents.validate_draft_with_context(profile_id)
        return VoiceAgentValidationRead(
            valid=not diagnostics,
            diagnostics=[
                ConfigurationDiagnosticRead(**item.__dict__)
                for item in diagnostics
            ],
            agent_backend_context_preview=context_preview,
            speaker_context_preview=speaker_context_preview,
        )

    @router.post("/voice-agents/{profile_id}/publish", response_model=VoiceAgentRevisionRead)
    async def publish_voice_agent(
        profile_id: str,
        payload: PublishRequest,
    ) -> VoiceAgentRevisionRead:
        value = await voice_agents.publish(
            profile_id,
            expected_revision=payload.expected_revision,
        )
        return voice_agent_revision(value)

    @router.get(
        "/voice-agents/{profile_id}/active",
        response_model=VoiceAgentRevisionRead,
    )
    async def active_voice_agent(profile_id: str) -> VoiceAgentRevisionRead:
        return voice_agent_revision(voice_agents.current(profile_id))

    @router.get(
        "/voice-agents/{profile_id}/revisions",
        response_model=list[VoiceAgentRevisionRead],
    )
    async def list_voice_agent_revisions(
        profile_id: str,
    ) -> list[VoiceAgentRevisionRead]:
        return [
            voice_agent_revision(item)
            for item in await voice_agents.list_revisions(profile_id)
        ]

    @router.post(
        "/voice-agents/{profile_id}/revisions/{revision}/restore",
        response_model=VoiceAgentDraftRead,
    )
    async def restore_voice_agent_revision(
        profile_id: str,
        revision: int,
        payload: RestoreRequest,
    ) -> VoiceAgentDraftRead:
        value = await voice_agents.restore_as_draft(
            profile_id,
            revision,
            expected_revision=payload.expected_revision,
        )
        return VoiceAgentDraftRead(**value.__dict__)

    return router
