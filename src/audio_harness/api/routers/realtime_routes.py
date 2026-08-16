from __future__ import annotations

from fastapi import APIRouter, Response, status

from audio_harness.realtime_routes.service import RealtimeRouteService
from audio_harness.schemas.realtime_routes import (
    RealtimeRouteRead,
    RealtimeRouteUpsert,
)


def create_realtime_route_router(routes: RealtimeRouteService) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["realtime-routes"])

    @router.get("/realtime-routes", response_model=list[RealtimeRouteRead])
    async def list_realtime_routes() -> list[RealtimeRouteRead]:
        return [RealtimeRouteRead(**item.__dict__) for item in routes.list()]

    @router.get(
        "/realtime-routes/{model}",
        response_model=RealtimeRouteRead,
    )
    async def get_realtime_route(model: str) -> RealtimeRouteRead:
        return RealtimeRouteRead(**routes.get(model).__dict__)

    @router.put("/realtime-routes/{model}", response_model=RealtimeRouteRead)
    async def upsert_realtime_route(
        model: str,
        payload: RealtimeRouteUpsert,
    ) -> RealtimeRouteRead:
        if model != payload.model:
            raise ValueError("路径 model 与请求体 model 不一致")
        value = await routes.upsert(**payload.model_dump())
        return RealtimeRouteRead(**value.__dict__)

    @router.delete("/realtime-routes/{model}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_realtime_route(model: str) -> Response:
        await routes.delete(model)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router

