from __future__ import annotations

from fastapi import APIRouter, File, Form, Response, UploadFile, status

from audio_harness.schemas.runtime import (
    CapacitySettingsRead,
    CapacitySettingsUpdate,
    HealthResponse,
    RealtimeOverviewRead,
    RuntimeIssueRead,
    RuntimeLimiterRead,
    RuntimeOverviewRead,
    RuntimeStatusRead,
    VoiceAssetRead,
    WorkflowSettingsRead,
    WorkflowSettingsUpdate,
)
from audio_harness.configs import WorkflowRuntimeConfig
from audio_harness.serving.capacity import ServingCapacity
from audio_harness.serving.capacity_settings import (
    CapacitySettingsSnapshot,
    CapacitySettingsStore,
)
from audio_harness.serving.status import RuntimeOverview, ServingStatusService
from audio_harness.voice_agents.voices import VoiceAssetStore
from audio_harness.workflows.settings import WorkflowSettingsSnapshot, WorkflowSettingsStore


def workflow_settings_response(value: WorkflowSettingsSnapshot) -> WorkflowSettingsRead:
    return WorkflowSettingsRead(
        **value.config.model_dump(),
        revision=value.revision,
        updated_at=value.updated_at,
    )


def capacity_settings_response(value: CapacitySettingsSnapshot) -> CapacitySettingsRead:
    return CapacitySettingsRead(
        **value.config.model_dump(),
        revision=value.revision,
        updated_at=value.updated_at,
    )


def readiness_response(overview: RuntimeOverview) -> HealthResponse:
    return HealthResponse(
        status="ready" if overview.ready else "not_ready",
        database="ready" if overview.control_plane == "ready" else "unavailable",
        problems=[
            issue.message
            for issue in overview.issues
            if issue.scope == "control_plane"
        ],
    )


def create_runtime_router(
    *,
    serving_status: ServingStatusService,
    workflow_settings: WorkflowSettingsStore,
    capacity_settings: CapacitySettingsStore,
    capacity: ServingCapacity,
    voices: VoiceAssetStore,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["runtime"])

    @router.get("/settings/workflow", response_model=WorkflowSettingsRead)
    async def get_workflow_settings() -> WorkflowSettingsRead:
        return workflow_settings_response(await workflow_settings.get())

    @router.put("/settings/workflow", response_model=WorkflowSettingsRead)
    async def update_workflow_settings(
        payload: WorkflowSettingsUpdate,
    ) -> WorkflowSettingsRead:
        snapshot = await workflow_settings.update(
            config=WorkflowRuntimeConfig.model_validate(
                payload.model_dump(exclude={"revision"})
            ),
            expected_revision=payload.revision,
        )
        return workflow_settings_response(snapshot)

    @router.get("/settings/capacity", response_model=CapacitySettingsRead)
    async def get_capacity_settings() -> CapacitySettingsRead:
        return capacity_settings_response(await capacity_settings.get())

    @router.put("/settings/capacity", response_model=CapacitySettingsRead)
    async def update_capacity_settings(
        payload: CapacitySettingsUpdate,
    ) -> CapacitySettingsRead:
        from audio_harness.configs import CapacityConfig

        return capacity_settings_response(
            await capacity_settings.update(
                config=CapacityConfig.model_validate(
                    payload.model_dump(exclude={"revision"})
                ),
                expected_revision=payload.revision,
            )
        )

    @router.get("/runtime/status", response_model=RuntimeStatusRead)
    async def runtime_status() -> RuntimeStatusRead:
        def read(limiter) -> RuntimeLimiterRead:
            return RuntimeLimiterRead(
                active=limiter.active,
                capacity=limiter.capacity,
                rejected=limiter.rejected,
            )

        return RuntimeStatusRead(
            realtime=read(capacity.realtime_sessions),
            webrtc=read(capacity.webrtc_sessions),
            asr=read(capacity.asr),
            embedding=read(capacity.embedding),
            qdrant=read(capacity.qdrant),
            responses=read(capacity.responses),
            tts=read(capacity.tts),
            started_at=capacity.started_at,
        )

    @router.get("/runtime/overview", response_model=RuntimeOverviewRead)
    async def runtime_overview() -> RuntimeOverviewRead:
        overview = await serving_status.snapshot()
        return RuntimeOverviewRead(
            status=overview.status,
            control_plane=overview.control_plane,
            realtime=RealtimeOverviewRead(**overview.realtime.__dict__),
            issues=[RuntimeIssueRead(**item.__dict__) for item in overview.issues],
        )

    @router.get("/voices", response_model=list[VoiceAssetRead])
    async def list_voices() -> list[VoiceAssetRead]:
        return [VoiceAssetRead(**item.__dict__) for item in await voices.list()]

    @router.post("/voices", response_model=VoiceAssetRead, status_code=status.HTTP_201_CREATED)
    async def create_voice(
        name: str = Form(...),
        ref_text: str | None = Form(default=None),
        file: UploadFile = File(...),
    ) -> VoiceAssetRead:
        return VoiceAssetRead(
            **(
                await voices.create(
                    name=name,
                    wav=await file.read(),
                    ref_text=ref_text,
                )
            ).__dict__
        )

    @router.delete("/voices/{voice_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_voice(voice_id: str) -> Response:
        await voices.delete(voice_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(status="ok", database="not_checked")

    @router.get("/health/ready", response_model=HealthResponse)
    async def readiness(response: Response) -> HealthResponse:
        overview = await serving_status.snapshot()
        if not overview.ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return readiness_response(overview)

    return router
