from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from audio_harness.api.schemas import (
    BindingRead,
    BindingUpdate,
    EngineCreate,
    EngineList,
    EnginePatch,
    EngineRead,
    ErrorResponse,
    HealthResponse,
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
from audio_harness.database.session import Database
from audio_harness.domain import EngineUnavailableError
from audio_harness.engines.router import EngineRouter


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


def create_management_app(
    *,
    database: Database,
    catalog: EngineCatalog,
    engine_router: EngineRouter,
    web_dist: Path | None = None,
) -> FastAPI:
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

    app.include_router(router)
    resolved_web_dist = (
        web_dist
        if web_dist is not None
        else Path(__file__).resolve().parents[2] / "web" / "dist"
    )
    if (resolved_web_dist / "index.html").is_file():
        app.mount(
            "/",
            StaticFiles(directory=resolved_web_dist, html=True),
            name="web",
        )
    else:

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def web_not_built() -> HTMLResponse:
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
