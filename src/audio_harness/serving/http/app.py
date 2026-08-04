from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode

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
from openai.types.realtime import (
    RealtimeSessionCreateRequest,
    RealtimeTranscriptionSessionCreateRequest,
)
from pydantic import ValidationError

from audio_harness.api.schemas import (
    CapacitySettingsRead,
    CapacitySettingsUpdate,
    EngineCreate,
    EngineList,
    EngineValidationRead,
    EngineValidationResultRead,
    EngineUsageRead,
    EnginePatch,
    EngineRead,
    ErrorResponse,
    HealthResponse,
    ModelRouteRead,
    ModelRouteUpsert,
    NodePortRead,
    NodeTypeRead,
    PipelineActivateRequest,
    PipelineDiagnosticRead,
    PipelineDraftRead,
    PipelineDraftUpdate,
    PipelineRevisionRead,
    PipelineRestoreRequest,
    PipelineValidationRead,
    WorkflowSettingsRead,
    WorkflowSettingsUpdate,
    WorkflowCreate,
    WorkflowPatch,
    WorkflowRead,
    WorkflowStageStatusRead,
    WorkflowStatusRead,
    RuntimeLimiterRead,
    RuntimeStatusRead,
    TestSessionCreate,
    TestSessionRead,
    VoiceAssetRead,
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
    EngineKind,
    EngineStatus,
)
from audio_harness.capacity_settings import (
    CapacityRevisionConflictError,
    CapacitySettingsSnapshot,
    CapacitySettingsStore,
)
from audio_harness.capacity import ServingCapacity
from audio_harness.config import CapacityConfig, WorkflowRuntimeConfig
from audio_harness.database.session import Database
from audio_harness.domain import CapacityExceededError, EngineUnavailableError
from audio_harness.engines.router import EngineRouter
from audio_harness.pipelines.dag import (
    GraphValidationError,
    default_node_registry,
)
from audio_harness.pipelines.service import (
    PipelineActivationError,
    PipelineError,
    PipelineRevisionConflictError,
    PipelineRevisionNotFoundError,
    PipelineStore,
    ModelRouteNotFoundError,
    WorkflowNotFoundError,
    WorkflowConflictError,
    definition_engine_ids,
)
from audio_harness.pipelines.stages import (
    StageWorkflowDefinition,
    default_stage_workflow,
)
from audio_harness.serving.realtime import RealtimeTranscriptionService
from audio_harness.serving.webrtc import WebRtcCallService
from audio_harness.serving.webrtc.service import WebRtcCallNotFoundError
from audio_harness.serving.test_sessions import (
    TEST_SESSION_COOKIE,
    TestSessionBroker,
)
from audio_harness.workflow_settings import (
    WorkflowRevisionConflictError,
    WorkflowSettingsValidationError,
    WorkflowSettingsSnapshot,
    WorkflowSettingsStore,
)
from audio_harness.workflow_types import GRPC_DEFAULT_MODEL
from audio_harness.voices import VoiceAssetNotFoundError, VoiceAssetStore


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
        credential_env=asset.credential_env,
        api_key=asset.api_key,
        enabled=asset.enabled,
        status=asset.status,
        image=asset.image,
        description=asset.description,
        status_basis=asset.status_basis,
        status_observed_at=asset.status_observed_at,
        last_success_at=asset.last_success_at,
        probe_latency_ms=asset.probe_latency_ms,
        consecutive_failures=asset.consecutive_failures,
        next_probe_at=asset.next_probe_at,
        source=asset.source,
        details=asset.details,
        revision=asset.revision,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
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
    pipeline_store: PipelineStore | None = None,
    workflow_settings: WorkflowSettingsStore | None = None,
    capacity_settings: CapacitySettingsStore | None = None,
    capacity: ServingCapacity | None = None,
    realtime_service: RealtimeTranscriptionService | None = None,
    webrtc_service: WebRtcCallService | None = None,
    max_message_bytes: int = 4 * 1024 * 1024,
    web_dist: Path | None = None,
    voice_store: VoiceAssetStore | None = None,
) -> FastAPI:
    workflow_settings = workflow_settings or WorkflowSettingsStore(
        database.session_factory,
        max_message_bytes=max_message_bytes,
    )
    capacity_settings = capacity_settings or CapacitySettingsStore(
        database.session_factory
    )
    capacity = capacity or ServingCapacity(CapacityConfig())
    pipeline_store = pipeline_store or PipelineStore(
        database.session_factory,
        engine_router=engine_router,
        registry=default_node_registry(),
    )
    app = FastAPI(
        title="Audio Harness Orchestration API",
        version="1.0.0",
    )
    router = APIRouter(prefix="/api/v1")
    test_sessions = TestSessionBroker()
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

    @app.exception_handler(PipelineRevisionConflictError)
    async def handle_pipeline_conflict(
        _request: Request,
        exc: PipelineRevisionConflictError,
    ) -> JSONResponse:
        return _error(409, exc.code, str(exc))

    @app.exception_handler(PipelineRevisionNotFoundError)
    async def handle_pipeline_not_found(
        _request: Request,
        exc: PipelineRevisionNotFoundError,
    ) -> JSONResponse:
        return _error(404, exc.code, str(exc))

    @app.exception_handler(WorkflowNotFoundError)
    async def handle_workflow_not_found(
        _request: Request,
        exc: WorkflowNotFoundError,
    ) -> JSONResponse:
        return _error(404, exc.code, str(exc))

    @app.exception_handler(WorkflowConflictError)
    async def handle_workflow_resource_conflict(
        _request: Request,
        exc: WorkflowConflictError,
    ) -> JSONResponse:
        return _error(409, exc.code, str(exc))

    @app.exception_handler(ModelRouteNotFoundError)
    async def handle_route_not_found(
        _request: Request,
        exc: ModelRouteNotFoundError,
    ) -> JSONResponse:
        return _error(404, exc.code, str(exc))

    @app.exception_handler(VoiceAssetNotFoundError)
    async def handle_voice_not_found(
        _request: Request,
        exc: VoiceAssetNotFoundError,
    ) -> JSONResponse:
        return _error(404, "voice_not_found", str(exc))

    @app.exception_handler(PipelineActivationError)
    async def handle_pipeline_activation(
        _request: Request,
        exc: PipelineActivationError,
    ) -> JSONResponse:
        return _error(503, exc.code, str(exc))

    @app.exception_handler(GraphValidationError)
    async def handle_graph_validation(
        _request: Request,
        exc: GraphValidationError,
    ) -> JSONResponse:
        return _error(
            422,
            "pipeline_invalid",
            "处理链校验失败",
            [
                {
                    "code": item.code,
                    "message": item.message,
                    "node_id": item.node_id,
                    "edge_id": item.edge_id,
                    "port": item.port,
                }
                for item in exc.diagnostics
            ],
        )

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

    @app.exception_handler(PipelineError)
    async def handle_pipeline_error(
        _request: Request,
        exc: PipelineError,
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

    @router.post(
        "/engines/validate",
        response_model=EngineValidationRead,
        responses=errors,
    )
    async def validate_engines() -> EngineValidationRead:
        results = await catalog.validate_enabled_assets()
        return EngineValidationRead(
            results=[
                EngineValidationResultRead(
                    engine_id=item.asset.id,
                    name=item.asset.name,
                    revision=item.asset.revision,
                    outcome=item.outcome,
                    message=item.message,
                    latency_ms=item.latency_ms,
                )
                for item in results
            ]
        )

    @router.post(
        "/engines/{asset_id}/probe",
        response_model=EngineValidationResultRead,
        responses=errors,
    )
    async def probe_engine(asset_id: str) -> EngineValidationResultRead:
        item = await catalog.validate_asset(asset_id)
        return EngineValidationResultRead(
            engine_id=item.asset.id,
            name=item.asset.name,
            revision=item.asset.revision,
            outcome=item.outcome,
            message=item.message,
            latency_ms=item.latency_ms,
        )

    @router.get(
        "/engines/{asset_id}/usage",
        response_model=EngineUsageRead,
        responses=errors,
    )
    async def get_engine_usage(asset_id: str) -> EngineUsageRead:
        await catalog.get_asset(asset_id)
        return EngineUsageRead(
            engine_id=asset_id,
            references=list(await pipeline_store.asset_references(asset_id)),
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

    def draft_response(
        draft,
    ) -> PipelineDraftRead:
        active = pipeline_store.current_or_none(draft.workflow_id)
        return PipelineDraftRead(
            workflow_id=draft.workflow_id,
            definition=draft.definition,
            revision=draft.revision,
            updated_at=draft.updated_at,
            active_revision=active.revision if active is not None else None,
        )

    def revision_response(
        snapshot,
    ) -> PipelineRevisionRead:
        return PipelineRevisionRead(
            definition=snapshot.definition,
            revision=snapshot.revision,
            activated_at=snapshot.activated_at,
            engine_revisions={
                node_id: engine.revision
                for node_id, node in snapshot.plan.nodes.items()
                if (engine := node.engine_snapshot) is not None
            },
        )

    @router.get(
        "/node-types",
        response_model=list[NodeTypeRead],
    )
    async def list_node_types() -> list[NodeTypeRead]:
        return [
            NodeTypeRead(
                type=node_type.type,
                display_name=node_type.display_name,
                input_ports={
                    name: NodePortRead(
                        payload_type=port.payload_type,
                        required=port.required,
                        aggregate=port.aggregate,
                    )
                    for name, port in node_type.input_ports.items()
                },
                output_ports={
                    name: NodePortRead(
                        payload_type=port.payload_type,
                        required=port.required,
                        aggregate=port.aggregate,
                    )
                    for name, port in node_type.output_ports.items()
                },
                config_schema=node_type.config_model.model_json_schema(),
                allowed_failure_policies=[
                    policy.value
                    for policy in node_type.allowed_failure_policies
                ],
                default_failure_policy=(
                    node_type.default_failure_policy.value
                ),
                required_api=(
                    node_type.required_api.value
                    if node_type.required_api is not None
                    else None
                ),
                max_instances=node_type.max_instances,
                source=node_type.source,
                sink=node_type.sink,
                version=node_type.version,
            )
            for node_type in pipeline_store.registry.all()
        ]

    def workflow_summary_response(snapshot) -> WorkflowRead:
        return WorkflowRead(
            id=snapshot.id,
            slug=snapshot.slug,
            name=snapshot.name,
            kind=snapshot.kind,
            description=snapshot.description,
            draft_revision=snapshot.draft_revision,
            active_revision=snapshot.active_revision,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
        )

    def model_route_response(snapshot) -> ModelRouteRead:
        return ModelRouteRead(
            model=snapshot.model,
            workflow_id=snapshot.workflow_id,
            visibility=snapshot.visibility,
            updated_at=snapshot.updated_at,
        )

    @router.get("/workflows", response_model=list[WorkflowRead])
    async def list_workflows() -> list[WorkflowRead]:
        return [
            workflow_summary_response(item)
            for item in await pipeline_store.list_workflows()
        ]

    @router.post(
        "/workflows",
        response_model=PipelineDraftRead,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_workflow(payload: WorkflowCreate) -> PipelineDraftRead:
        return draft_response(
            await pipeline_store.create_workflow(
                slug=payload.slug,
                name=payload.name,
                kind=payload.kind,
                description=payload.description,
                definition=(
                    payload.definition
                    if payload.definition is not None
                    else default_stage_workflow(payload.kind)
                ),
            )
        )

    @router.get("/workflows/{workflow_id}", response_model=WorkflowRead)
    async def get_workflow(workflow_id: str) -> WorkflowRead:
        return workflow_summary_response(
            await pipeline_store.get_workflow_summary(workflow_id)
        )

    @router.patch(
        "/workflows/{workflow_id}",
        response_model=PipelineDraftRead,
    )
    async def patch_workflow(
        workflow_id: str,
        payload: WorkflowPatch,
    ) -> PipelineDraftRead:
        return draft_response(
            await pipeline_store.update_workflow_metadata(
                workflow_id,
                **payload.model_dump(exclude_unset=True),
            )
        )

    @router.delete(
        "/workflows/{workflow_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_workflow(workflow_id: str) -> Response:
        await pipeline_store.delete_workflow(workflow_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/workflows/{workflow_id}/draft",
        response_model=PipelineDraftRead,
    )
    async def get_workflow_draft(workflow_id: str) -> PipelineDraftRead:
        return draft_response(await pipeline_store.get_draft(workflow_id))

    @router.put(
        "/workflows/{workflow_id}/draft",
        response_model=PipelineDraftRead,
    )
    async def put_workflow_draft(
        workflow_id: str,
        payload: PipelineDraftUpdate,
    ) -> PipelineDraftRead:
        return draft_response(
            await pipeline_store.update_draft(
                workflow_id=workflow_id,
                definition=payload.definition,
                expected_revision=payload.expected_revision,
            )
        )

    @router.post(
        "/workflows/{workflow_id}/validate",
        response_model=PipelineValidationRead,
    )
    async def validate_workflow_draft(
        workflow_id: str,
    ) -> PipelineValidationRead:
        try:
            plan = await pipeline_store.validate_draft(workflow_id)
        except GraphValidationError as exc:
            return PipelineValidationRead(
                valid=False,
                diagnostics=[
                    PipelineDiagnosticRead(
                        code=item.code,
                        message=item.message,
                        node_id=item.node_id,
                        edge_id=item.edge_id,
                        port=item.port,
                    )
                    for item in exc.diagnostics
                ],
            )
        return PipelineValidationRead(
            valid=True,
            diagnostics=[],
            topological_order=list(plan.topological_order),
        )

    @router.post(
        "/workflows/{workflow_id}/activate",
        response_model=PipelineRevisionRead,
    )
    async def activate_workflow(
        workflow_id: str,
        payload: PipelineActivateRequest,
    ) -> PipelineRevisionRead:
        return revision_response(
            await pipeline_store.activate(
                workflow_id=workflow_id,
                expected_revision=payload.expected_revision,
            )
        )

    @router.post(
        "/workflows/{workflow_id}/publish",
        response_model=PipelineRevisionRead,
    )
    async def publish_workflow(
        workflow_id: str,
        payload: PipelineActivateRequest,
    ) -> PipelineRevisionRead:
        """校验、准备所有引擎并原子发布当前草稿。"""
        draft = await pipeline_store.get_draft(workflow_id)
        for asset_id in sorted(
            definition_engine_ids(draft.definition.model_dump(mode="json"))
        ):
            result = await catalog.validate_asset(asset_id)
            if result.outcome != "passed":
                raise PipelineActivationError(
                    f"发布前引擎探测失败: {result.asset.name}: "
                    f"{result.message or result.outcome}"
                )
        return revision_response(
            await pipeline_store.activate(
                workflow_id=workflow_id,
                expected_revision=payload.expected_revision,
            )
        )

    @router.get(
        "/workflows/{workflow_id}/revisions",
        response_model=list[PipelineRevisionRead],
    )
    async def list_workflow_revisions(
        workflow_id: str,
    ) -> list[PipelineRevisionRead]:
        return [
            revision_response(item)
            for item in await pipeline_store.list_revisions(workflow_id)
        ]

    @router.post(
        "/workflows/{workflow_id}/revisions/{revision}/restore",
        response_model=PipelineDraftRead,
    )
    async def restore_workflow_revision(
        workflow_id: str,
        revision: int,
        payload: PipelineRestoreRequest,
    ) -> PipelineDraftRead:
        return draft_response(
            await pipeline_store.restore_revision_as_draft(
                workflow_id=workflow_id,
                revision_number=revision,
                expected_revision=payload.expected_revision,
            )
        )

    @router.get(
        "/workflows/{workflow_id}/status",
        response_model=WorkflowStatusRead,
    )
    async def get_workflow_status(workflow_id: str) -> WorkflowStatusRead:
        draft = await pipeline_store.get_draft(workflow_id)
        active = pipeline_store.current_or_none(workflow_id)
        diagnostics: list[PipelineDiagnosticRead] = []
        try:
            await pipeline_store.compile(draft.definition, kind=draft.kind)
        except GraphValidationError as exc:
            diagnostics = [
                PipelineDiagnosticRead(
                    code=item.code,
                    message=item.message,
                    node_id=item.node_id,
                    edge_id=item.edge_id,
                    port=item.port,
                    stage=item.node_id,
                )
                for item in exc.diagnostics
            ]
        except ValueError as exc:
            diagnostics = [
                PipelineDiagnosticRead(
                    code="invalid_stage_definition",
                    message=str(exc),
                )
            ]

        stage_bindings: list[tuple[str, str | None]] = []
        if isinstance(draft.definition, StageWorkflowDefinition):
            for stage_name in (
                "physical_vad",
                "semantic_vad",
                "asr",
                "responses_agent",
                "duplex_tts",
            ):
                stage = getattr(draft.definition.stages, stage_name)
                if stage is not None:
                    stage_bindings.append((stage_name, stage.engine_id))
        else:
            stage_bindings = [
                (node.type, node.engine_id)
                for node in draft.definition.nodes
                if node.engine_id is not None
            ]

        stage_states: list[WorkflowStageStatusRead] = []
        all_healthy = True
        for stage_name, engine_id in stage_bindings:
            if engine_id is None:
                all_healthy = False
                stage_states.append(
                    WorkflowStageStatusRead(
                        stage=stage_name,
                        engine_id=None,
                        health="missing",
                        reason="尚未绑定引擎",
                    )
                )
                continue
            try:
                asset = await catalog.get_asset(engine_id)
            except AssetNotFoundError:
                all_healthy = False
                stage_states.append(
                    WorkflowStageStatusRead(
                        stage=stage_name,
                        engine_id=engine_id,
                        health="missing",
                        reason="绑定的引擎不存在",
                    )
                )
                continue
            healthy = asset.status is EngineStatus.HEALTHY and asset.enabled
            all_healthy = all_healthy and healthy
            stage_states.append(
                WorkflowStageStatusRead(
                    stage=stage_name,
                    engine_id=engine_id,
                    engine_name=asset.name,
                    health=(asset.status.value if asset.enabled else "disabled"),
                    reason=asset.status_basis,
                    checked_at=asset.status_observed_at,
                )
            )

        has_unpublished = (
            active is None
            or active.definition.model_dump(mode="json")
            != draft.definition.model_dump(mode="json")
        )
        if diagnostics:
            state = "incomplete"
        elif active is None:
            state = "unpublished"
        elif has_unpublished:
            state = "unpublished_changes"
        elif all_healthy:
            state = "published_healthy"
        else:
            state = "published_degraded"
        models = sorted(
            route.model
            for route in pipeline_store.model_routes()
            if route.workflow_id == workflow_id and route.visibility.value == "public"
        )
        return WorkflowStatusRead(
            workflow_id=workflow_id,
            state=state,
            draft_revision=draft.revision,
            active_revision=active.revision if active is not None else None,
            has_unpublished_changes=has_unpublished,
            public_models=models,
            diagnostics=diagnostics,
            stages=stage_states,
        )

    @router.get(
        "/workflows/{workflow_id}/active",
        response_model=PipelineRevisionRead,
    )
    async def get_active_workflow(workflow_id: str) -> PipelineRevisionRead:
        return revision_response(pipeline_store.current(workflow_id))

    @router.get(
        "/workflows/{workflow_id}/revisions/{revision}",
        response_model=PipelineRevisionRead,
    )
    async def get_workflow_revision(
        workflow_id: str,
        revision: int,
    ) -> PipelineRevisionRead:
        return revision_response(
            await pipeline_store.get_revision(revision, workflow_id)
        )

    @router.get("/model-routes", response_model=list[ModelRouteRead])
    async def list_model_routes() -> list[ModelRouteRead]:
        return [
            model_route_response(item)
            for item in pipeline_store.model_routes()
        ]

    @router.put(
        "/model-routes/{model:path}",
        response_model=ModelRouteRead,
    )
    async def put_model_route(
        model: str,
        payload: ModelRouteUpsert,
    ) -> ModelRouteRead:
        if payload.model != model:
            raise HTTPException(
                status_code=422,
                detail="路径 model 与请求体 model 不一致",
            )
        return model_route_response(
            await pipeline_store.upsert_model_route(
                model=payload.model,
                workflow_id=payload.workflow_id,
                visibility=payload.visibility,
            )
        )

    @router.delete(
        "/model-routes/{model:path}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_model_route(model: str) -> Response:
        await pipeline_store.delete_model_route(model)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if voice_store is not None:

        def voice_response(snapshot) -> VoiceAssetRead:
            return VoiceAssetRead(
                id=snapshot.id,
                name=snapshot.name,
                ref_text=snapshot.ref_text,
                sample_rate=snapshot.sample_rate,
                duration_seconds=snapshot.duration_seconds,
                created_at=snapshot.created_at,
            )

        @router.get("/voices", response_model=list[VoiceAssetRead])
        async def list_voices() -> list[VoiceAssetRead]:
            return [
                voice_response(item)
                for item in await voice_store.list()
            ]

        @router.post(
            "/voices",
            response_model=VoiceAssetRead,
            status_code=status.HTTP_201_CREATED,
        )
        async def create_voice(request: Request) -> VoiceAssetRead:
            form = await request.form()
            upload = form.get("file")
            name = form.get("name")
            ref_text = form.get("ref_text")
            if not isinstance(name, str) or not name.strip():
                raise HTTPException(422, "name 不能为空")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(422, "file 必须是 WAV 文件")
            wav = await upload.read(voice_store.max_upload_bytes + 1)
            try:
                snapshot = await voice_store.create(
                    name=name,
                    wav=wav,
                    ref_text=ref_text if isinstance(ref_text, str) else None,
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return voice_response(snapshot)

        @router.delete(
            "/voices/{voice_id}",
            status_code=status.HTTP_204_NO_CONTENT,
        )
        async def delete_voice(voice_id: str) -> Response:
            await voice_store.delete(voice_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/pipeline",
        response_model=PipelineDraftRead,
    )
    async def get_pipeline() -> PipelineDraftRead:
        return draft_response(await pipeline_store.get_draft())

    @router.put(
        "/pipeline/draft",
        response_model=PipelineDraftRead,
        responses=errors,
    )
    async def put_pipeline_draft(
        payload: PipelineDraftUpdate,
    ) -> PipelineDraftRead:
        return draft_response(
            await pipeline_store.update_draft(
                definition=payload.definition,
                expected_revision=payload.expected_revision,
            )
        )

    @router.post(
        "/pipeline/validate",
        response_model=PipelineValidationRead,
    )
    async def validate_pipeline() -> PipelineValidationRead:
        try:
            plan = await pipeline_store.validate_draft()
        except GraphValidationError as exc:
            return PipelineValidationRead(
                valid=False,
                diagnostics=[
                    PipelineDiagnosticRead(
                        code=item.code,
                        message=item.message,
                        node_id=item.node_id,
                        edge_id=item.edge_id,
                        port=item.port,
                    )
                    for item in exc.diagnostics
                ],
            )
        return PipelineValidationRead(
            valid=True,
            diagnostics=[],
            topological_order=list(plan.topological_order),
        )

    @router.post(
        "/pipeline/activate",
        response_model=PipelineRevisionRead,
        responses=errors,
    )
    async def activate_pipeline(
        payload: PipelineActivateRequest,
    ) -> PipelineRevisionRead:
        return revision_response(
            await pipeline_store.activate(
                expected_revision=payload.expected_revision
            )
        )

    @router.get(
        "/pipeline/active",
        response_model=PipelineRevisionRead,
    )
    async def get_active_pipeline() -> PipelineRevisionRead:
        return revision_response(pipeline_store.current())

    @router.get(
        "/pipeline/revisions/{revision}",
        response_model=PipelineRevisionRead,
    )
    async def get_pipeline_revision(
        revision: int,
    ) -> PipelineRevisionRead:
        return revision_response(
            await pipeline_store.get_revision(revision)
        )

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
        config = WorkflowRuntimeConfig.model_validate(
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

    @router.get("/runtime/status", response_model=RuntimeStatusRead)
    async def get_runtime_status() -> RuntimeStatusRead:
        def limiter_response(limiter) -> RuntimeLimiterRead:
            return RuntimeLimiterRead(
                active=limiter.active,
                capacity=limiter.capacity,
                rejected=limiter.rejected,
            )

        return RuntimeStatusRead(
            grpc=limiter_response(capacity.grpc_sessions),
            realtime=limiter_response(capacity.realtime_sessions),
            webrtc=limiter_response(capacity.webrtc_sessions),
            asr=limiter_response(capacity.asr),
            responses=limiter_response(capacity.responses),
            tts=limiter_response(capacity.tts),
            started_at=capacity.started_at,
        )

    @router.post("/test-sessions", response_model=TestSessionRead)
    async def create_test_session(
        payload: TestSessionCreate,
        request: Request,
    ) -> Response:
        pipeline_store.current(payload.workflow_id)
        workflow = await pipeline_store.get_workflow_summary(payload.workflow_id)
        public_models = sorted(
            route.model
            for route in pipeline_store.model_routes()
            if route.workflow_id == payload.workflow_id
            and route.visibility.value == "public"
        )
        if not public_models:
            return _error(
                409,
                "workflow_not_routable",
                "工作流尚未绑定公开模型名",
            )
        model = workflow.slug if workflow.slug in public_models else public_models[0]
        credential = await test_sessions.create(
            workflow_id=payload.workflow_id,
            model=model,
            protocol=payload.protocol,
        )
        base = "/v1/realtime" if payload.protocol == "wss" else "/v1/realtime/calls"
        endpoint = f"{base}?{urlencode({'model': model})}"
        body = TestSessionRead(
            id=credential.id,
            workflow_id=payload.workflow_id,
            model=model,
            protocol=payload.protocol,
            endpoint=endpoint,
            expires_at=credential.expires_at,
        )
        response = JSONResponse(content=body.model_dump(mode="json"))
        response.set_cookie(
            TEST_SESSION_COOKIE,
            credential.token,
            max_age=60,
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
            path="/v1/realtime",
        )
        return response

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
        pipeline_revision: int | None = None
        database_status = "ready"
        try:
            await database.ping()
        except Exception:
            database_status = "unavailable"
            problems.append("SQLite 不可用")
        data_ready = False
        if database_status == "ready":
            try:
                active = pipeline_store.resolve_model(
                    GRPC_DEFAULT_MODEL,
                    allow_internal=True,
                )
            except PipelineError:
                problems.append("grpc-default 未指向已激活工作流")
            except Exception:
                problems.append("数据面状态读取失败")
            else:
                pipeline_revision = active.revision
                data_ready = True
        response = HealthResponse(
            status="ready" if not problems else "not_ready",
            database=database_status,
            data_plane="ready" if data_ready else "not_ready",
            problems=problems,
            pipeline_revision=pipeline_revision,
            nodes=[],
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
            test_authorized = await test_sessions.consume(
                websocket.cookies.get(TEST_SESSION_COOKIE),
                model=model,
                protocol="wss",
            )
            await realtime_service.handle(
                websocket,
                model=model,
                diagnostics_enabled=diagnostics,
                test_authorized=test_authorized,
            )

    if webrtc_service is not None:

        @app.post("/v1/realtime/calls")
        async def create_realtime_call(
            request: Request,
            authorization: str = Header(default=""),
            diagnostics: bool = Query(default=False),
            model: str | None = Query(default=None, min_length=1),
        ) -> Response:
            static_authorized = webrtc_service.authorize(authorization)
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
                session_payload = (
                    json.loads(session_json)
                    if session_json is not None
                    else None
                )
                if session_payload is None:
                    session_config = None
                elif session_payload.get("type") == "realtime":
                    session_config = RealtimeSessionCreateRequest.model_validate(
                        session_payload
                    )
                else:
                    session_config = (
                        RealtimeTranscriptionSessionCreateRequest.model_validate(
                            session_payload
                        )
                    )
                selected_model = model
                if selected_model is None and session_payload is not None:
                    if session_payload.get("type") == "realtime":
                        selected_model = session_payload.get("model")
                    else:
                        audio_input = (
                            (session_payload.get("audio") or {}).get("input")
                            or {}
                        )
                        selected_model = (
                            (audio_input.get("transcription") or {}).get(
                                "model"
                            )
                        )
                selected_model = selected_model or "gpt-4o-transcribe"
                if not static_authorized and not await test_sessions.consume(
                    request.cookies.get(TEST_SESSION_COOKIE),
                    model=selected_model,
                    protocol="webrtc",
                ):
                    return _error(401, "unauthorized", "Unauthorized")
                call_arguments = {
                    "offer_sdp": offer_sdp,
                    "session": session_config,
                    "diagnostics_enabled": diagnostics,
                }
                if (
                    model is not None
                    or (
                        session_payload is not None
                        and session_payload.get("type") == "realtime"
                    )
                ):
                    call_arguments["model"] = selected_model
                answer = await webrtc_service.create_call(**call_arguments)
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
