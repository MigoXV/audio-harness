from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import (
    APIRouter,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai.types.realtime import RealtimeTranscriptionSessionCreateRequest
from pydantic import ValidationError

from audio_harness.api.schemas import (
    BindingRead,
    BindingUpdate,
    CapacitySettingsRead,
    CapacitySettingsUpdate,
    EngineCreate,
    EngineList,
    EnginePatch,
    EngineRead,
    ErrorResponse,
    HealthResponse,
    WorkflowSettingsRead,
    WorkflowSettingsUpdate,
)
from audio_harness.api.service import (
    AssetNotFoundError,
    CatalogConflictError,
    CatalogError,
    EngineCatalog,
)
from audio_harness.assets import (
    EngineApi,
    EngineAssetData,
    EngineBindingData,
    EngineKind,
    EngineRole,
)
from audio_harness.capacity_settings import (
    CapacityRevisionConflictError,
    CapacitySettingsSnapshot,
    CapacitySettingsStore,
)
from audio_harness.config import CapacityConfig, WorkflowConfig
from audio_harness.database.session import Database
from audio_harness.domain import CapacityExceededError, EngineUnavailableError
from audio_harness.engines.router import EngineRouter
from audio_harness.serving.realtime import RealtimeTranscriptionService
from audio_harness.serving.webrtc import WebRtcCallService
from audio_harness.serving.webrtc.service import WebRtcCallNotFoundError
from audio_harness.workflow_settings import (
    WorkflowRevisionConflictError,
    WorkflowSettingsValidationError,
    WorkflowSettingsSnapshot,
    WorkflowSettingsStore,
)


def engine_response(asset: EngineAssetData) -> EngineRead:
    if asset.created_at is None or asset.updated_at is None:
        raise RuntimeError("持久化引擎缺少时间戳")
    return EngineRead(
        id=asset.id,
        slug=asset.slug,
        name=asset.name,
        kind=asset.kind,
        api=asset.api,
        protocol=asset.protocol,
        target=asset.target,
        connect_timeout_seconds=asset.connect_timeout_seconds,
        request_timeout_seconds=asset.request_timeout_seconds,
        enabled=asset.enabled,
        status=asset.status,
        image=asset.image,
        description=asset.description,
        status_basis=asset.status_basis,
        status_observed_at=asset.status_observed_at,
        source=asset.source,
        details=asset.details,
        revision=asset.revision,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def binding_response(binding: EngineBindingData) -> BindingRead:
    return BindingRead(
        role=binding.role,
        engine=(
            engine_response(binding.engine)
            if binding.engine is not None
            else None
        ),
        generation=binding.generation,
        updated_at=binding.updated_at,
    )


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
    database: Database,
    catalog: EngineCatalog,
    engine_router: EngineRouter,
    workflow_settings: WorkflowSettingsStore | None = None,
    capacity_settings: CapacitySettingsStore | None = None,
    realtime_service: RealtimeTranscriptionService | None = None,
    webrtc_service: WebRtcCallService | None = None,
    max_message_bytes: int = 4 * 1024 * 1024,
    web_dist: Path | None = None,
) -> FastAPI:
    workflow_settings = workflow_settings or WorkflowSettingsStore(
        database.session_factory,
        max_message_bytes=max_message_bytes,
    )
    capacity_settings = capacity_settings or CapacitySettingsStore(
        database.session_factory
    )
    app = FastAPI(
        title="Audio Harness Engine Control API",
        version="1.0.0",
    )
    router = APIRouter(prefix="/api/v1")
    errors = {
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    }

    @app.exception_handler(AssetNotFoundError)
    async def handle_not_found(
        _request: Request,
        exc: AssetNotFoundError,
    ) -> JSONResponse:
        return _error(404, exc.code, str(exc))

    @app.exception_handler(CatalogConflictError)
    async def handle_conflict(
        _request: Request,
        exc: CatalogConflictError,
    ) -> JSONResponse:
        return _error(409, exc.code, str(exc))

    @app.exception_handler(WorkflowRevisionConflictError)
    async def handle_workflow_conflict(
        _request: Request,
        exc: WorkflowRevisionConflictError,
    ) -> JSONResponse:
        return _error(409, exc.code, str(exc))

    @app.exception_handler(CapacityRevisionConflictError)
    async def handle_capacity_conflict(
        _request: Request,
        exc: CapacityRevisionConflictError,
    ) -> JSONResponse:
        return _error(409, exc.code, str(exc))

    @app.exception_handler(WorkflowSettingsValidationError)
    async def handle_workflow_validation(
        _request: Request,
        exc: WorkflowSettingsValidationError,
    ) -> JSONResponse:
        return _error(422, exc.code, str(exc))

    @app.exception_handler(EngineUnavailableError)
    async def handle_unavailable(
        _request: Request,
        exc: EngineUnavailableError,
    ) -> JSONResponse:
        return _error(503, "engine_unavailable", str(exc))

    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return _error(
            422,
            "validation_error",
            "请求参数不合法",
            jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(CatalogError)
    async def handle_catalog_error(
        _request: Request,
        exc: CatalogError,
    ) -> JSONResponse:
        return _error(400, exc.code, str(exc))

    @router.post(
        "/engines",
        response_model=EngineRead,
        status_code=status.HTTP_201_CREATED,
        responses=errors,
    )
    async def create_engine(payload: EngineCreate) -> EngineRead:
        return engine_response(await catalog.create_asset(payload))

    @router.get("/engines", response_model=EngineList)
    async def list_engines(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        kind: EngineKind | None = None,
        api: EngineApi | None = None,
        enabled: bool | None = None,
        q: str | None = Query(default=None, max_length=200),
    ) -> EngineList:
        items, total = await catalog.list_assets(
            limit=limit,
            offset=offset,
            kind=kind,
            api=api,
            enabled=enabled,
            query=q,
        )
        return EngineList(
            items=[engine_response(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/engines/{asset_id}",
        response_model=EngineRead,
        responses=errors,
    )
    async def get_engine(asset_id: str) -> EngineRead:
        return engine_response(await catalog.get_asset(asset_id))

    @router.patch(
        "/engines/{asset_id}",
        response_model=EngineRead,
        responses=errors,
    )
    async def update_engine(
        asset_id: str,
        payload: EnginePatch,
    ) -> EngineRead:
        return engine_response(await catalog.update_asset(asset_id, payload))

    @router.delete(
        "/engines/{asset_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=errors,
    )
    async def delete_engine(asset_id: str) -> Response:
        await catalog.delete_asset(asset_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/engine-bindings",
        response_model=list[BindingRead],
    )
    async def list_engine_bindings() -> list[BindingRead]:
        return [
            binding_response(binding)
            for binding in await catalog.list_bindings()
        ]

    @router.get(
        "/engine-bindings/{role}",
        response_model=BindingRead,
    )
    async def get_engine_binding(role: EngineRole) -> BindingRead:
        return binding_response(await catalog.get_binding(role))

    @router.put(
        "/engine-bindings/{role}",
        response_model=BindingRead,
        responses=errors,
    )
    async def put_engine_binding(
        role: EngineRole,
        payload: BindingUpdate,
    ) -> BindingRead:
        return binding_response(await catalog.bind(role, payload.engine_id))

    @router.delete(
        "/engine-bindings/{role}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_engine_binding(role: EngineRole) -> Response:
        await catalog.unbind(role)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    def workflow_response(
        snapshot: WorkflowSettingsSnapshot,
    ) -> WorkflowSettingsRead:
        return WorkflowSettingsRead(
            **snapshot.config.model_dump(),
            revision=snapshot.revision,
            updated_at=snapshot.updated_at,
        )

    @router.get(
        "/workflow-settings",
        response_model=WorkflowSettingsRead,
    )
    async def get_workflow_settings() -> WorkflowSettingsRead:
        return workflow_response(await workflow_settings.get())

    @router.put(
        "/workflow-settings",
        response_model=WorkflowSettingsRead,
        responses={409: {"model": ErrorResponse}},
    )
    async def put_workflow_settings(
        payload: WorkflowSettingsUpdate,
    ) -> WorkflowSettingsRead:
        config = WorkflowConfig.model_validate(
            payload.model_dump(exclude={"revision"})
        )
        snapshot = await workflow_settings.update(
            config=config,
            expected_revision=payload.revision,
        )
        return workflow_response(snapshot)

    def capacity_response(
        snapshot: CapacitySettingsSnapshot,
    ) -> CapacitySettingsRead:
        return CapacitySettingsRead(
            **snapshot.config.model_dump(),
            revision=snapshot.revision,
            updated_at=snapshot.updated_at,
        )

    @router.get(
        "/capacity-settings",
        response_model=CapacitySettingsRead,
    )
    async def get_capacity_settings() -> CapacitySettingsRead:
        return capacity_response(await capacity_settings.get())

    @router.put(
        "/capacity-settings",
        response_model=CapacitySettingsRead,
        responses={409: {"model": ErrorResponse}},
    )
    async def put_capacity_settings(
        payload: CapacitySettingsUpdate,
    ) -> CapacitySettingsRead:
        config = CapacityConfig.model_validate(
            payload.model_dump(exclude={"revision"})
        )
        snapshot = await capacity_settings.update(
            config=config,
            expected_revision=payload.revision,
        )
        return capacity_response(snapshot)

    @app.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(
            status="ok",
            database="not_checked",
            data_plane="not_checked",
        )

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
    )
    async def readiness() -> Response:
        problems: list[str] = []
        database_status = "ready"
        try:
            await database.ping()
        except Exception:
            database_status = "unavailable"
            problems.append("SQLite 不可用")
        data_ready = False
        if database_status == "ready":
            try:
                data_ready, data_problems = await engine_router.readiness()
                problems.extend(data_problems)
            except Exception:
                problems.append("数据面状态读取失败")
        response = HealthResponse(
            status="ready" if not problems else "not_ready",
            database=database_status,
            data_plane="ready" if data_ready else "not_ready",
            problems=problems,
        )
        return JSONResponse(
            status_code=200 if not problems else 503,
            content=response.model_dump(mode="json"),
        )

    if realtime_service is not None:

        @app.websocket("/v1/realtime")
        async def realtime_transcription(
            websocket: WebSocket,
            model: str = Query(min_length=1),
            diagnostics: bool = Query(default=False),
        ) -> None:
            await realtime_service.handle(
                websocket,
                model=model,
                diagnostics_enabled=diagnostics,
            )

    if webrtc_service is not None:

        @app.post("/v1/realtime/calls")
        async def create_realtime_call(
            request: Request,
            authorization: str = Header(default=""),
            diagnostics: bool = Query(default=False),
        ) -> Response:
            if not webrtc_service.authorize(authorization):
                return _error(401, "unauthorized", "Unauthorized")
            try:
                form = await request.form()
            except Exception as exc:
                return _error(
                    400,
                    "invalid_multipart",
                    f"multipart 表单解析失败: {exc}",
                )
            sdp_part = form.get("sdp")
            if sdp_part is None:
                return _error(400, "invalid_sdp", "缺少 sdp 表单字段")
            if hasattr(sdp_part, "read"):
                offer = await sdp_part.read(max_message_bytes + 1)
            elif isinstance(sdp_part, str):
                offer = sdp_part.encode("utf-8")
            else:
                return _error(400, "invalid_sdp", "sdp 必须是文本或文件")
            if len(offer) > max_message_bytes:
                return _error(
                    400,
                    "sdp_too_large",
                    "sdp 超过 max_message_bytes",
                )
            try:
                offer_sdp = offer.decode("utf-8")
                session_part = form.get("session")
                if hasattr(session_part, "read"):
                    session_bytes = await session_part.read(
                        max_message_bytes + 1
                    )
                    if len(session_bytes) > max_message_bytes:
                        return _error(
                            400,
                            "session_too_large",
                            "session 超过 max_message_bytes",
                        )
                    session_json = session_bytes.decode("utf-8")
                elif session_part is None or isinstance(session_part, str):
                    session_json = session_part
                else:
                    return _error(
                        400,
                        "invalid_session",
                        "session 必须是 JSON 文本或文件",
                    )
                session_config = (
                    RealtimeTranscriptionSessionCreateRequest.model_validate(
                        json.loads(session_json)
                    )
                    if session_json is not None
                    else None
                )
                answer = await webrtc_service.create_call(
                    offer_sdp=offer_sdp,
                    session=session_config,
                    diagnostics_enabled=diagnostics,
                )
            except UnicodeDecodeError:
                return _error(
                    400,
                    "invalid_encoding",
                    "sdp 和 session 必须是 UTF-8 文本",
                )
            except (json.JSONDecodeError, ValidationError) as exc:
                return _error(
                    400,
                    "invalid_session",
                    f"session 配置不合法: {exc}",
                )
            except CapacityExceededError as exc:
                return _error(429, "capacity_exceeded", str(exc))
            except ValueError as exc:
                return _error(400, "invalid_webrtc_offer", str(exc))
            except asyncio.TimeoutError:
                return _error(408, "negotiation_timeout", "WebRTC 协商超时")
            return Response(
                content=answer.sdp,
                status_code=status.HTTP_201_CREATED,
                media_type="application/sdp",
                headers={
                    "Location": f"/v1/realtime/calls/{answer.call_id}",
                },
            )

        @app.post("/v1/realtime/calls/{call_id}/hangup")
        async def hangup_realtime_call(
            call_id: str,
            authorization: str = Header(default=""),
        ) -> Response:
            if not webrtc_service.authorize(authorization):
                return _error(401, "unauthorized", "Unauthorized")
            try:
                await webrtc_service.hangup(call_id)
            except WebRtcCallNotFoundError:
                return _error(404, "call_not_found", "WebRTC call 不存在")
            return Response(status_code=status.HTTP_200_OK)

    app.include_router(router)
    resolved_web_dist = (
        web_dist
        if web_dist is not None
        else Path(__file__).resolve().parents[3] / "web" / "dist"
    )
    if (resolved_web_dist / "index.html").is_file():
        assets_dir = resolved_web_dist / "assets"
        if assets_dir.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=assets_dir),
                name="web-assets",
            )

        @app.get(
            "/{spa_path:path}",
            response_class=FileResponse,
            include_in_schema=False,
        )
        async def web_spa(spa_path: str) -> FileResponse:
            first_segment = spa_path.partition("/")[0]
            if first_segment in {
                "api",
                "docs",
                "health",
                "openapi.json",
                "redoc",
                "v1",
            }:
                raise HTTPException(status_code=404)
            candidate = (resolved_web_dist / spa_path).resolve()
            try:
                candidate.relative_to(resolved_web_dist.resolve())
            except ValueError:
                raise HTTPException(status_code=404) from None
            if candidate.is_file() and candidate.name != "index.html":
                return FileResponse(candidate)
            return FileResponse(resolved_web_dist / "index.html")
    else:

        @app.get(
            "/{spa_path:path}",
            response_class=HTMLResponse,
            include_in_schema=False,
        )
        async def web_not_built(spa_path: str) -> HTMLResponse:
            if spa_path.partition("/")[0] in {
                "api",
                "docs",
                "health",
                "openapi.json",
                "redoc",
                "v1",
            }:
                raise HTTPException(status_code=404)
            return HTMLResponse(
                content=(
                    "<!doctype html><html lang=\"zh-CN\"><head>"
                    "<meta charset=\"utf-8\"><title>Audio Harness</title>"
                    "</head><body><main><h1>Audio Harness</h1>"
                    "<p>Web 配置界面尚未构建。请在 src/web 下执行 "
                    "<code>pnpm install && pnpm build</code>。</p>"
                    "</main></body></html>"
                ),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
    return app
