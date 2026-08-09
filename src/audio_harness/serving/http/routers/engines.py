from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from audio_harness.api.schemas import (
    EngineCreate,
    EngineList,
    EngineModelListRead,
    EnginePatch,
    EngineRead,
    EngineUsageRead,
    EngineValidationRead,
    EngineValidationResultRead,
)
from audio_harness.api.service import EngineCatalog
from audio_harness.assets import EngineApi, EngineAssetData, EngineKind


def engine_response(asset: EngineAssetData) -> EngineRead:
    if asset.created_at is None or asset.updated_at is None:
        raise RuntimeError("持久化引擎缺少时间戳")
    return EngineRead.model_validate(asset.__dict__)


def validation_response(result) -> EngineValidationResultRead:
    return EngineValidationResultRead(
        engine_id=result.asset.id,
        name=result.asset.name,
        revision=result.asset.revision,
        outcome=result.outcome,
        message=result.message,
        latency_ms=result.latency_ms,
    )


def create_engine_router(catalog: EngineCatalog) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["engines"])

    @router.get("/engines", response_model=EngineList)
    async def list_engines(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        kind: EngineKind | None = None,
        api: EngineApi | None = None,
        enabled: bool | None = None,
        q: str | None = None,
    ) -> EngineList:
        assets, total = await catalog.list_assets(
            limit=limit,
            offset=offset,
            kind=kind,
            api=api,
            enabled=enabled,
            query=q,
        )
        return EngineList(
            items=[engine_response(asset) for asset in assets],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.post("/engines", response_model=EngineRead, status_code=status.HTTP_201_CREATED)
    async def create_engine(payload: EngineCreate) -> EngineRead:
        return engine_response(await catalog.create_asset(payload.model_dump()))

    @router.get("/engines/{engine_id}", response_model=EngineRead)
    async def get_engine(engine_id: str) -> EngineRead:
        return engine_response(await catalog.get_asset(engine_id))

    @router.get(
        "/engines/{engine_id}/models",
        response_model=EngineModelListRead,
    )
    async def list_engine_models(engine_id: str) -> EngineModelListRead:
        asset, models = await catalog.list_models(engine_id)
        return EngineModelListRead(
            engine_id=asset.id,
            engine_revision=asset.revision,
            items=list(models),
        )

    @router.patch("/engines/{engine_id}", response_model=EngineRead)
    async def patch_engine(engine_id: str, payload: EnginePatch) -> EngineRead:
        return engine_response(
            await catalog.update_asset(
                engine_id,
                payload.model_dump(exclude_unset=True),
            )
        )

    @router.delete("/engines/{engine_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_engine(engine_id: str) -> Response:
        await catalog.delete_asset(engine_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/engines/{engine_id}/validate", response_model=EngineValidationRead)
    async def validate_engine(engine_id: str) -> EngineValidationRead:
        return EngineValidationRead(
            results=[validation_response(await catalog.validate_asset(engine_id))]
        )

    @router.post("/engines/validate", response_model=EngineValidationRead)
    async def validate_engines() -> EngineValidationRead:
        return EngineValidationRead(
            results=[
                validation_response(result)
                for result in await catalog.validate_enabled_assets()
            ]
        )

    @router.get("/engines/{engine_id}/usage", response_model=EngineUsageRead)
    async def engine_usage(engine_id: str) -> EngineUsageRead:
        return EngineUsageRead(
            engine_id=engine_id,
            references=list(await catalog.references(engine_id)),
        )

    return router
