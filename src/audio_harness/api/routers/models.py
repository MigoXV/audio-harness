from fastapi import APIRouter

from audio_harness.serving.model_catalog import RealtimeModelCatalog


def create_model_router(models: RealtimeModelCatalog) -> APIRouter:
    router = APIRouter(tags=["models"])

    @router.get("/v1/models")
    async def list_models() -> dict[str, object]:
        return {
            "object": "list",
            "data": [item.openai_document() for item in await models.list_models()],
        }

    return router
