from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from audio_harness.api.routers.data_plane import create_data_plane_router
from audio_harness.api.routers.engines import create_engine_router
from audio_harness.api.routers.realtime_routes import create_realtime_route_router
from audio_harness.api.routers.runtime import (
    create_runtime_router,
    readiness_response,
)
from audio_harness.api.routers.workflows import create_workflow_router
from audio_harness.engines.catalog import (
    AssetNotFoundError,
    CatalogConflictError,
    CatalogError,
    EngineCatalog,
    EngineModelCapabilityError,
    EngineModelDiscoveryError,
    EngineModelDiscoveryTimeoutError,
)
from audio_harness.realtime_routes.service import (
    RealtimeRouteConflictError,
    RealtimeRouteNotFoundError,
    RealtimeRouteService,
)
from audio_harness.serving.capacity import ServingCapacity
from audio_harness.serving.capacity_settings import (
    CapacityRevisionConflictError,
    CapacitySettingsStore,
)
from audio_harness.errors import CapacityExceededError
from audio_harness.serving.realtime import RealtimeTranscriptionService
from audio_harness.serving.status import ServingStatusService
from audio_harness.serving.webrtc import WebRtcCallService
from audio_harness.serving.webrtc.service import WebRtcCallNotFoundError
from audio_harness.voice_agents.repository import (
    VoiceAgentConflictError,
    VoiceAgentNotFoundError,
    VoiceAgentRepositoryError,
)
from audio_harness.voice_agents.service import (
    VoiceAgentNotActiveError,
    VoiceAgentPublishError,
    VoiceAgentService,
)
from audio_harness.voice_agents.voices import VoiceAssetNotFoundError, VoiceAssetStore
from audio_harness.workflows.settings import (
    WorkflowRevisionConflictError,
    WorkflowSettingsStore,
    WorkflowSettingsValidationError,
)
from audio_harness.workflows.compiler import AudioGraphValidationError
from audio_harness.workflows.repository import (
    AudioWorkflowConflictError,
    AudioWorkflowNotFoundError,
    AudioWorkflowRepositoryError,
    AudioWorkflowRevisionNotFoundError,
)
from audio_harness.workflows.service import (
    AudioWorkflowNotActiveError,
    AudioWorkflowPublishError,
    AudioWorkflowService,
)

logger = logging.getLogger(__name__)

DEFAULT_WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


def _error(status_code: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
            }
        },
    )


def create_http_app(
    *,
    catalog: EngineCatalog,
    workflows: AudioWorkflowService,
    voice_agents: VoiceAgentService,
    routes: RealtimeRouteService,
    serving_status: ServingStatusService,
    workflow_settings: WorkflowSettingsStore,
    capacity_settings: CapacitySettingsStore,
    capacity: ServingCapacity,
    realtime_service: RealtimeTranscriptionService,
    webrtc_service: WebRtcCallService,
    max_message_bytes: int,
    voice_store: VoiceAssetStore,
    web_dist: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Audio Harness", version="1.0.0")

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return _error(422, "request_validation_failed", "请求参数不合法", exc.errors())

    @app.exception_handler(AssetNotFoundError)
    @app.exception_handler(AudioWorkflowNotFoundError)
    @app.exception_handler(AudioWorkflowRevisionNotFoundError)
    @app.exception_handler(VoiceAgentNotFoundError)
    @app.exception_handler(RealtimeRouteNotFoundError)
    @app.exception_handler(VoiceAssetNotFoundError)
    @app.exception_handler(WebRtcCallNotFoundError)
    async def not_found_handler(_request: Request, exc: Exception) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(EngineModelCapabilityError)
    async def model_capability_handler(
        _request: Request,
        exc: EngineModelCapabilityError,
    ) -> JSONResponse:
        return _error(409, exc.code, str(exc))

    @app.exception_handler(EngineModelDiscoveryTimeoutError)
    async def model_discovery_timeout_handler(
        _request: Request,
        exc: EngineModelDiscoveryTimeoutError,
    ) -> JSONResponse:
        return _error(504, exc.code, str(exc))

    @app.exception_handler(EngineModelDiscoveryError)
    async def model_discovery_handler(
        _request: Request,
        exc: EngineModelDiscoveryError,
    ) -> JSONResponse:
        return _error(502, exc.code, str(exc))

    @app.exception_handler(CatalogConflictError)
    @app.exception_handler(AudioWorkflowConflictError)
    @app.exception_handler(VoiceAgentConflictError)
    @app.exception_handler(WorkflowRevisionConflictError)
    @app.exception_handler(CapacityRevisionConflictError)
    async def conflict_handler(_request: Request, exc: Exception) -> JSONResponse:
        return _error(409, "revision_conflict", str(exc))

    @app.exception_handler(RealtimeRouteConflictError)
    async def route_conflict_handler(
        _request: Request,
        exc: RealtimeRouteConflictError,
    ) -> JSONResponse:
        return _error(
            409,
            "realtime_route_conflict",
            str(exc),
            {
                "model": exc.current.model,
                "target_type": exc.current.target_type.value,
                "target_id": exc.current.target_id,
                "visibility": exc.current.visibility.value,
            },
        )

    @app.exception_handler(AudioGraphValidationError)
    async def graph_validation_handler(
        _request: Request,
        exc: AudioGraphValidationError,
    ) -> JSONResponse:
        return _error(
            422,
            "audio_graph_invalid",
            str(exc),
            [item.__dict__ for item in exc.diagnostics],
        )

    @app.exception_handler(AudioWorkflowNotActiveError)
    @app.exception_handler(VoiceAgentNotActiveError)
    @app.exception_handler(AudioWorkflowPublishError)
    @app.exception_handler(VoiceAgentPublishError)
    @app.exception_handler(WorkflowSettingsValidationError)
    async def precondition_handler(_request: Request, exc: Exception) -> JSONResponse:
        return _error(412, "precondition_failed", str(exc))

    @app.exception_handler(CapacityExceededError)
    async def capacity_handler(_request: Request, exc: Exception) -> JSONResponse:
        return _error(429, "capacity_exceeded", str(exc))

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return _error(400, "invalid_request", str(exc))

    @app.exception_handler(CatalogError)
    @app.exception_handler(AudioWorkflowRepositoryError)
    @app.exception_handler(VoiceAgentRepositoryError)
    async def service_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        return _error(500, "service_error", str(exc))

    app.include_router(create_engine_router(catalog))
    app.include_router(create_workflow_router(workflows, voice_agents))
    app.include_router(create_realtime_route_router(routes))
    app.include_router(
        create_runtime_router(
            workflows=workflows,
            serving_status=serving_status,
            workflow_settings=workflow_settings,
            capacity_settings=capacity_settings,
            capacity=capacity,
            voices=voice_store,
        )
    )
    app.include_router(
        create_data_plane_router(
            realtime=realtime_service,
            webrtc=webrtc_service,
            max_message_bytes=max_message_bytes,
        )
    )

    @app.get("/health/live", include_in_schema=False)
    async def root_liveness() -> JSONResponse:
        return JSONResponse({"status": "ok", "database": "not_checked", "data_plane": "not_checked", "problems": []})

    @app.get("/health/ready", include_in_schema=False)
    async def root_readiness() -> JSONResponse:
        overview = await serving_status.snapshot()
        probe = readiness_response(overview)
        return JSONResponse(
            status_code=503 if not overview.ready else 200,
            content=probe.model_dump(mode="json"),
        )

    resolved_web_dist = (
        web_dist if web_dist is not None else DEFAULT_WEB_DIST
    ).resolve()
    if (resolved_web_dist / "index.html").is_file():
        web_root = resolved_web_dist
        assets_dir = resolved_web_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="web-assets")

        @app.get(
            "/{spa_path:path}",
            response_class=FileResponse,
            response_model=None,
            include_in_schema=False,
        )
        async def web_spa(spa_path: str):
            if spa_path.partition("/")[0] in {"api", "docs", "openapi.json", "v1", "health"}:
                return _error(404, "not_found", "请求路径不存在")
            static_file = (web_root / spa_path).resolve()
            if static_file.is_relative_to(web_root) and static_file.is_file():
                return FileResponse(static_file)
            return FileResponse(web_root / "index.html")

        logger.info("Audio Harness Web UI 已挂载: directory=%s", web_root)
    else:
        logger.warning(
            "Audio Harness Web UI 未挂载，index.html 不存在: directory=%s",
            resolved_web_dist,
        )

    return app
