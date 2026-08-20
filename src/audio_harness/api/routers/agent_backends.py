from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from audio_harness.agent_backends.catalog import AgentBackendCatalog
from audio_harness.agent_backends.models import (
    AgentBackendAssetData,
    AgentBackendCapabilities,
    AgentBackendKind,
    AgentBackendProtocol,
)
from audio_harness.schemas.agent_backends import (
    AgentBackendCapabilitiesRead,
    AgentBackendCreate,
    AgentBackendList,
    AgentBackendPatch,
    AgentBackendPluginRead,
    AgentBackendRead,
    AgentBackendUsageRead,
    AgentBackendValidationRead,
)


def backend_response(asset: AgentBackendAssetData) -> AgentBackendRead:
    if asset.created_at is None or asset.updated_at is None:
        raise RuntimeError("持久化 Agent Backend 缺少时间戳")
    return AgentBackendRead.model_validate(asset.__dict__)


def capabilities_response(
    asset: AgentBackendAssetData,
    value: AgentBackendCapabilities,
) -> AgentBackendCapabilitiesRead:
    return AgentBackendCapabilitiesRead(
        backend_id=asset.id,
        backend_revision=asset.revision,
        wire_version=value.wire_version,
        provider=value.provider,
        model=value.model,
        cwd=value.cwd,
        presets=list(value.presets),
        plugins=[
            AgentBackendPluginRead(
                entry_id=item.entry_id,
                module_name=item.module_name,
                enabled=item.enabled,
                phase=item.phase,
            )
            for item in value.plugins
        ],
    )


def create_agent_backend_router(catalog: AgentBackendCatalog) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["agent-backends"])

    @router.get("/agent-backends", response_model=AgentBackendList)
    async def list_agent_backends(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        kind: AgentBackendKind | None = None,
        protocol: AgentBackendProtocol | None = None,
        enabled: bool | None = None,
        q: str | None = None,
    ) -> AgentBackendList:
        assets, total = await catalog.list_assets(
            limit=limit,
            offset=offset,
            kind=kind,
            protocol=protocol,
            enabled=enabled,
            query=q,
        )
        return AgentBackendList(
            items=[backend_response(asset) for asset in assets],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.post(
        "/agent-backends",
        response_model=AgentBackendRead,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_agent_backend(
        payload: AgentBackendCreate,
    ) -> AgentBackendRead:
        return backend_response(
            await catalog.create_asset(payload.model_dump())
        )

    @router.get(
        "/agent-backends/{backend_id}",
        response_model=AgentBackendRead,
    )
    async def get_agent_backend(backend_id: str) -> AgentBackendRead:
        return backend_response(await catalog.get_asset(backend_id))

    @router.patch(
        "/agent-backends/{backend_id}",
        response_model=AgentBackendRead,
    )
    async def patch_agent_backend(
        backend_id: str,
        payload: AgentBackendPatch,
    ) -> AgentBackendRead:
        return backend_response(
            await catalog.update_asset(
                backend_id,
                payload.model_dump(exclude_unset=True),
            )
        )

    @router.delete(
        "/agent-backends/{backend_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_agent_backend(backend_id: str) -> Response:
        await catalog.delete_asset(backend_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/agent-backends/{backend_id}/validate",
        response_model=AgentBackendValidationRead,
    )
    async def validate_agent_backend(
        backend_id: str,
    ) -> AgentBackendValidationRead:
        result = await catalog.validate_asset(backend_id)
        return AgentBackendValidationRead(
            backend_id=result.asset.id,
            name=result.asset.name,
            revision=result.asset.revision,
            outcome=result.outcome,
            message=result.message,
            latency_ms=result.latency_ms,
        )

    @router.get(
        "/agent-backends/{backend_id}/capabilities",
        response_model=AgentBackendCapabilitiesRead,
    )
    async def get_agent_backend_capabilities(
        backend_id: str,
    ) -> AgentBackendCapabilitiesRead:
        asset, value = await catalog.capabilities(backend_id)
        return capabilities_response(asset, value)

    @router.get(
        "/agent-backends/{backend_id}/usage",
        response_model=AgentBackendUsageRead,
    )
    async def get_agent_backend_usage(
        backend_id: str,
    ) -> AgentBackendUsageRead:
        return AgentBackendUsageRead(
            backend_id=backend_id,
            references=list(await catalog.references(backend_id)),
        )

    return router
