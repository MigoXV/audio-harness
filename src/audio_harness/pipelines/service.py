from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.database.mappers import asset_data
from audio_harness.database.models import (
    EngineAsset,
    ModelRoute,
    Workflow,
    WorkflowActivation,
    WorkflowRevision,
    utc_now,
)
from audio_harness.engines.router import EngineRouter
from audio_harness.pipelines.dag import (
    CompiledPipelinePlan,
    GraphDiagnostic,
    GraphValidationError,
    NodeTypeRegistry,
    PhysicalVadNodeConfig,
    PipelineDefinition,
    compile_pipeline,
    compiled_plan_document,
    restore_compiled_plan,
)
from audio_harness.pipelines.stages import (
    WorkflowDefinition,
    expanded_pipeline,
    parse_workflow_definition,
)
from audio_harness.workflow_types import (
    DEFAULT_TRANSCRIPTION_WORKFLOW_ID,
    GRPC_DEFAULT_MODEL,
    ModelRouteVisibility,
    WorkflowKind,
)


class PipelineError(Exception):
    code = "pipeline_error"


class PipelineNotActiveError(PipelineError):
    code = "pipeline_not_active"


class PipelineRevisionConflictError(PipelineError):
    code = "pipeline_revision_conflict"


class PipelineRevisionNotFoundError(PipelineError):
    code = "pipeline_revision_not_found"


class PipelineActivationError(PipelineError):
    code = "pipeline_activation_failed"


class WorkflowNotFoundError(PipelineError):
    code = "workflow_not_found"


class WorkflowConflictError(PipelineError):
    code = "workflow_conflict"


class ModelRouteNotFoundError(PipelineError):
    code = "model_route_not_found"


_DESCRIPTION_UNSET = object()


@dataclass(frozen=True)
class PipelineDraftSnapshot:
    definition: WorkflowDefinition
    revision: int
    updated_at: datetime
    workflow_id: str = DEFAULT_TRANSCRIPTION_WORKFLOW_ID
    slug: str = DEFAULT_TRANSCRIPTION_WORKFLOW_ID
    name: str = "默认转写工作流"
    kind: WorkflowKind = WorkflowKind.TRANSCRIPTION
    description: str | None = None


@dataclass(frozen=True)
class ActivePipelineSnapshot:
    definition: WorkflowDefinition
    plan: CompiledPipelinePlan
    revision: int
    activated_at: datetime
    workflow_id: str = DEFAULT_TRANSCRIPTION_WORKFLOW_ID
    kind: WorkflowKind = WorkflowKind.TRANSCRIPTION


@dataclass(frozen=True)
class WorkflowSummary:
    id: str
    slug: str
    name: str
    kind: WorkflowKind
    description: str | None
    draft_revision: int
    active_revision: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ModelRouteSnapshot:
    model: str
    workflow_id: str
    visibility: ModelRouteVisibility
    updated_at: datetime


def definition_engine_ids(definition: dict[str, Any]) -> set[str]:
    if definition.get("schema_version") == 2:
        stages = definition.get("stages", {})
        if not isinstance(stages, dict):
            return set()
        return {
            str(stage["engine_id"])
            for stage in stages.values()
            if isinstance(stage, dict) and stage.get("engine_id")
        }
    return {
        str(node["engine_id"])
        for node in definition.get("nodes", [])
        if isinstance(node, dict) and node.get("engine_id")
    }


class PipelineStore:
    """多工作流存储；旧方法映射到默认转写工作流。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        engine_router: EngineRouter,
        registry: NodeTypeRegistry,
    ) -> None:
        self._session_factory = session_factory
        self._engine_router = engine_router
        self.registry = registry
        self._write_lock = asyncio.Lock()
        self._active: dict[str, ActivePipelineSnapshot] = {}
        self._routes: dict[str, ModelRouteSnapshot] = {}

    async def initialize(self) -> None:
        active: dict[str, ActivePipelineSnapshot] = {}
        routes: dict[str, ModelRouteSnapshot] = {}
        async with self._session_factory() as session:
            workflows = (
                await session.execute(select(Workflow))
            ).scalars().all()
            if not workflows:
                raise RuntimeError("多工作流数据库迁移尚未初始化")
            for workflow in workflows:
                activation = await session.get(
                    WorkflowActivation,
                    workflow.id,
                )
                if activation is None or activation.active_revision_id is None:
                    continue
                revision = await session.get(
                    WorkflowRevision,
                    activation.active_revision_id,
                )
                if revision is None or revision.workflow_id != workflow.id:
                    raise RuntimeError(
                        f"工作流 {workflow.id} 的激活修订不存在"
                    )
                definition = parse_workflow_definition(revision.definition)
                pipeline = expanded_pipeline(definition, workflow.kind)
                plan = restore_compiled_plan(
                    pipeline,
                    revision.compiled_plan,
                    registry=self.registry,
                )
                active[workflow.id] = ActivePipelineSnapshot(
                    workflow_id=workflow.id,
                    kind=workflow.kind,
                    definition=definition,
                    plan=plan,
                    revision=revision.revision,
                    activated_at=revision.activated_at,
                )
            result = await session.execute(select(ModelRoute))
            for route in result.scalars().all():
                routes[route.model] = ModelRouteSnapshot(
                    model=route.model,
                    workflow_id=route.workflow_id,
                    visibility=route.visibility,
                    updated_at=route.updated_at,
                )
        self._active = active
        self._routes = routes

    def current(
        self,
        workflow_id: str = DEFAULT_TRANSCRIPTION_WORKFLOW_ID,
    ) -> ActivePipelineSnapshot:
        current = self._active.get(workflow_id)
        if current is None:
            raise PipelineNotActiveError(
                f"工作流尚未激活: {workflow_id}"
            )
        return current

    def current_or_none(
        self,
        workflow_id: str = DEFAULT_TRANSCRIPTION_WORKFLOW_ID,
    ) -> ActivePipelineSnapshot | None:
        return self._active.get(workflow_id)

    def active_workflows(self) -> tuple[ActivePipelineSnapshot, ...]:
        return tuple(self._active[key] for key in sorted(self._active))

    def model_routes(self) -> tuple[ModelRouteSnapshot, ...]:
        return tuple(self._routes[key] for key in sorted(self._routes))

    def resolve_model(
        self,
        model: str,
        *,
        allow_internal: bool = False,
    ) -> ActivePipelineSnapshot:
        route = self._routes.get(model)
        if route is None:
            raise ModelRouteNotFoundError(f"模型路由不存在: {model}")
        if (
            route.visibility is ModelRouteVisibility.INTERNAL
            and not allow_internal
        ):
            raise ModelRouteNotFoundError(f"模型路由不存在: {model}")
        return self.current(route.workflow_id)

    def physical_vad_config(
        self,
        workflow_id: str = DEFAULT_TRANSCRIPTION_WORKFLOW_ID,
    ) -> PhysicalVadNodeConfig:
        active = self.current(workflow_id)
        node = next(
            (
                item
                for item in active.plan.nodes.values()
                if item.node_type.type == "physical_vad"
            ),
            None,
        )
        if node is None or not isinstance(node.config, PhysicalVadNodeConfig):
            raise PipelineNotActiveError(
                f"工作流缺少物理 VAD 配置: {workflow_id}"
            )
        return node.config

    async def list_workflows(self) -> tuple[WorkflowSummary, ...]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(Workflow).order_by(Workflow.created_at, Workflow.id)
            )
            values = []
            for workflow in result.scalars().all():
                active = self._active.get(workflow.id)
                values.append(
                    WorkflowSummary(
                        id=workflow.id,
                        slug=workflow.slug,
                        name=workflow.name,
                        kind=workflow.kind,
                        description=workflow.description,
                        draft_revision=workflow.draft_revision,
                        active_revision=(
                            active.revision if active is not None else None
                        ),
                        created_at=workflow.created_at,
                        updated_at=workflow.updated_at,
                    )
                )
            return tuple(values)

    async def get_workflow_summary(self, workflow_id: str) -> WorkflowSummary:
        values = await self.list_workflows()
        for value in values:
            if value.id == workflow_id:
                return value
        raise WorkflowNotFoundError(f"工作流不存在: {workflow_id}")

    async def _workflow(self, workflow_id: str) -> Workflow:
        async with self._session_factory() as session:
            workflow = await session.get(Workflow, workflow_id)
            if workflow is None:
                raise WorkflowNotFoundError(
                    f"工作流不存在: {workflow_id}"
                )
            return workflow

    async def create_workflow(
        self,
        *,
        slug: str,
        name: str,
        kind: WorkflowKind,
        definition: WorkflowDefinition,
        description: str | None = None,
    ) -> PipelineDraftSnapshot:
        now = utc_now()
        workflow_id = f"wf-{uuid4().hex}"
        async with self._write_lock:
            async with self._session_factory() as session:
                workflow = Workflow(
                    id=workflow_id,
                    slug=slug,
                    name=name,
                    kind=kind,
                    description=description,
                    draft_revision=1,
                    draft_definition=definition.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
                session.add(workflow)
                # ModelRoute 没有 ORM relationship 可供 SQLAlchemy 推导插入顺序；
                # 先落下父记录，避免后续查询触发 autoflush 时违反外键约束。
                try:
                    await session.flush()
                except IntegrityError as exc:
                    await session.rollback()
                    raise WorkflowConflictError(
                        f"工作流 slug 或模型路由已存在: {slug}"
                    ) from exc
                grpc_route = await session.get(ModelRoute, GRPC_DEFAULT_MODEL)
                session.add(
                    WorkflowActivation(
                        workflow_id=workflow_id,
                        active_revision_id=None,
                        updated_at=now,
                    )
                )
                public_route = ModelRoute(
                    model=slug,
                    workflow_id=workflow_id,
                    visibility=ModelRouteVisibility.PUBLIC,
                    created_at=now,
                    updated_at=now,
                )
                session.add(public_route)
                create_grpc_route = (
                    kind is WorkflowKind.TRANSCRIPTION and grpc_route is None
                )
                if create_grpc_route:
                    session.add(
                        ModelRoute(
                            model=GRPC_DEFAULT_MODEL,
                            workflow_id=workflow_id,
                            visibility=ModelRouteVisibility.INTERNAL,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                try:
                    await session.commit()
                except IntegrityError as exc:
                    await session.rollback()
                    raise WorkflowConflictError(
                        f"工作流 slug 或模型路由已存在: {slug}"
                    ) from exc
        self._routes[slug] = ModelRouteSnapshot(
            model=slug,
            workflow_id=workflow_id,
            visibility=ModelRouteVisibility.PUBLIC,
            updated_at=now,
        )
        if create_grpc_route:
            self._routes[GRPC_DEFAULT_MODEL] = ModelRouteSnapshot(
                model=GRPC_DEFAULT_MODEL,
                workflow_id=workflow_id,
                visibility=ModelRouteVisibility.INTERNAL,
                updated_at=now,
            )
        return PipelineDraftSnapshot(
            workflow_id=workflow_id,
            slug=slug,
            name=name,
            kind=kind,
            description=description,
            definition=definition,
            revision=1,
            updated_at=now,
        )

    async def get_draft(
        self,
        workflow_id: str = DEFAULT_TRANSCRIPTION_WORKFLOW_ID,
    ) -> PipelineDraftSnapshot:
        workflow = await self._workflow(workflow_id)
        return PipelineDraftSnapshot(
            workflow_id=workflow.id,
            slug=workflow.slug,
            name=workflow.name,
            kind=workflow.kind,
            description=workflow.description,
            definition=parse_workflow_definition(workflow.draft_definition),
            revision=workflow.draft_revision,
            updated_at=workflow.updated_at,
        )

    async def update_draft(
        self,
        *,
        definition: WorkflowDefinition,
        expected_revision: int,
        workflow_id: str = DEFAULT_TRANSCRIPTION_WORKFLOW_ID,
    ) -> PipelineDraftSnapshot:
        async with self._write_lock:
            async with self._session_factory() as session:
                workflow = await session.get(Workflow, workflow_id)
                if workflow is None:
                    raise WorkflowNotFoundError(
                        f"工作流不存在: {workflow_id}"
                    )
                if workflow.draft_revision != expected_revision:
                    raise PipelineRevisionConflictError(
                        "工作流草稿已经被其他操作更新"
                    )
                workflow.draft_definition = definition.model_dump(mode="json")
                workflow.draft_revision += 1
                workflow.updated_at = utc_now()
                await session.commit()
                await session.refresh(workflow)
                return PipelineDraftSnapshot(
                    workflow_id=workflow.id,
                    slug=workflow.slug,
                    name=workflow.name,
                    kind=workflow.kind,
                    description=workflow.description,
                    definition=definition,
                    revision=workflow.draft_revision,
                    updated_at=workflow.updated_at,
                )

    async def update_workflow_metadata(
        self,
        workflow_id: str,
        *,
        name: str | None = None,
        description: str | None | object = _DESCRIPTION_UNSET,
    ) -> PipelineDraftSnapshot:
        async with self._write_lock:
            async with self._session_factory() as session:
                workflow = await session.get(Workflow, workflow_id)
                if workflow is None:
                    raise WorkflowNotFoundError(
                        f"工作流不存在: {workflow_id}"
                    )
                if name is not None:
                    workflow.name = name
                if description is not _DESCRIPTION_UNSET:
                    workflow.description = cast(str | None, description)
                workflow.updated_at = utc_now()
                await session.commit()
        return await self.get_draft(workflow_id)

    async def _assets(self) -> dict[str, Any]:
        async with self._session_factory() as session:
            result = await session.execute(select(EngineAsset))
            return {
                model.id: asset_data(model)
                for model in result.scalars().all()
            }

    @staticmethod
    def _validate_workflow_kind(
        kind: WorkflowKind,
        definition: PipelineDefinition,
    ) -> None:
        node_types = [node.type for node in definition.nodes]
        diagnostics: list[GraphDiagnostic] = []
        agent_count = node_types.count("responses_agent")
        tts_count = node_types.count("duplex_tts")
        if kind is WorkflowKind.TRANSCRIPTION:
            if agent_count or tts_count:
                diagnostics.append(
                    GraphDiagnostic(
                        "workflow_kind_mismatch",
                        "转写工作流不能包含 Responses 或 TTS 节点",
                    )
                )
        elif agent_count != 1 or tts_count != 1:
            diagnostics.append(
                GraphDiagnostic(
                    "workflow_kind_mismatch",
                    "语音 Agent 工作流必须各包含一个 Responses "
                    "和 TTS 节点",
                )
            )
        if diagnostics:
            raise GraphValidationError(diagnostics)

    async def compile(
        self,
        definition: WorkflowDefinition,
        *,
        kind: WorkflowKind = WorkflowKind.TRANSCRIPTION,
    ) -> CompiledPipelinePlan:
        pipeline = expanded_pipeline(definition, kind)
        self._validate_workflow_kind(kind, pipeline)
        return compile_pipeline(
            pipeline,
            registry=self.registry,
            assets=await self._assets(),
        )

    async def validate_draft(
        self,
        workflow_id: str = DEFAULT_TRANSCRIPTION_WORKFLOW_ID,
    ) -> CompiledPipelinePlan:
        draft = await self.get_draft(workflow_id)
        return await self.compile(draft.definition, kind=draft.kind)

    async def activate(
        self,
        *,
        expected_revision: int,
        workflow_id: str = DEFAULT_TRANSCRIPTION_WORKFLOW_ID,
    ) -> ActivePipelineSnapshot:
        async with self._write_lock:
            draft = await self.get_draft(workflow_id)
            if draft.revision != expected_revision:
                raise PipelineRevisionConflictError(
                    "工作流草稿已经被其他操作更新"
                )
            plan = await self.compile(draft.definition, kind=draft.kind)
            try:
                await self._engine_router.prepare_all(plan.engine_snapshots)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise PipelineActivationError(
                    f"工作流引擎准备失败: {exc}"
                ) from exc

            now = utc_now()
            async with self._session_factory() as session:
                workflow = await session.get(Workflow, workflow_id)
                if (
                    workflow is None
                    or workflow.draft_revision != expected_revision
                ):
                    raise PipelineRevisionConflictError(
                        "工作流草稿已经被其他操作更新"
                    )
                for snapshot in plan.engine_snapshots:
                    asset = await session.get(EngineAsset, snapshot.id)
                    if (
                        asset is None
                        or asset.revision != snapshot.revision
                        or not asset.enabled
                    ):
                        raise PipelineRevisionConflictError(
                            "工作流引用的引擎在验证期间发生变化"
                        )
                latest = await session.scalar(
                    select(func.max(WorkflowRevision.revision)).where(
                        WorkflowRevision.workflow_id == workflow_id
                    )
                )
                revision_number = int(latest or 0) + 1
                revision = WorkflowRevision(
                    workflow_id=workflow_id,
                    revision=revision_number,
                    definition=draft.definition.model_dump(mode="json"),
                    compiled_plan=compiled_plan_document(plan),
                    created_at=now,
                    activated_at=now,
                )
                session.add(revision)
                await session.flush()
                activation = await session.get(
                    WorkflowActivation,
                    workflow_id,
                )
                if activation is None:
                    raise RuntimeError("工作流激活记录不存在")
                activation.active_revision_id = revision.id
                activation.updated_at = now
                await session.commit()

            previous = self._active.get(workflow_id)
            snapshot = ActivePipelineSnapshot(
                workflow_id=workflow_id,
                kind=draft.kind,
                definition=draft.definition,
                plan=plan,
                revision=revision_number,
                activated_at=now,
            )
            self._active[workflow_id] = snapshot
            if previous is not None:
                active_keys = {
                    (item.id, item.revision)
                    for active in self._active.values()
                    for item in active.plan.engine_snapshots
                }
                for item in previous.plan.engine_snapshots:
                    if (item.id, item.revision) not in active_keys:
                        await self._engine_router.retire_snapshot(
                            item.id,
                            item.revision,
                        )
            return snapshot

    async def get_revision(
        self,
        revision_number: int,
        workflow_id: str = DEFAULT_TRANSCRIPTION_WORKFLOW_ID,
    ) -> ActivePipelineSnapshot:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WorkflowRevision, Workflow)
                .join(Workflow, Workflow.id == WorkflowRevision.workflow_id)
                .where(
                    WorkflowRevision.workflow_id == workflow_id,
                    WorkflowRevision.revision == revision_number,
                )
            )
            row = result.one_or_none()
            if row is None:
                raise PipelineRevisionNotFoundError(
                    f"工作流修订不存在: {workflow_id}/{revision_number}"
                )
            revision, workflow = row
            definition = parse_workflow_definition(revision.definition)
            pipeline = expanded_pipeline(definition, workflow.kind)
            return ActivePipelineSnapshot(
                workflow_id=workflow_id,
                kind=workflow.kind,
                definition=definition,
                plan=restore_compiled_plan(
                    pipeline,
                    revision.compiled_plan,
                    registry=self.registry,
                ),
                revision=revision.revision,
                activated_at=revision.activated_at,
            )

    async def list_revisions(
        self,
        workflow_id: str,
    ) -> tuple[ActivePipelineSnapshot, ...]:
        await self._workflow(workflow_id)
        async with self._session_factory() as session:
            result = await session.execute(
                select(WorkflowRevision.revision)
                .where(WorkflowRevision.workflow_id == workflow_id)
                .order_by(WorkflowRevision.revision.desc())
            )
            numbers = tuple(result.scalars().all())
        return tuple(
            [
                await self.get_revision(number, workflow_id)
                for number in numbers
            ]
        )

    async def restore_revision_as_draft(
        self,
        *,
        workflow_id: str,
        revision_number: int,
        expected_revision: int,
    ) -> PipelineDraftSnapshot:
        revision = await self.get_revision(revision_number, workflow_id)
        return await self.update_draft(
            workflow_id=workflow_id,
            definition=revision.definition,
            expected_revision=expected_revision,
        )

    async def upsert_model_route(
        self,
        *,
        model: str,
        workflow_id: str,
        visibility: ModelRouteVisibility,
    ) -> ModelRouteSnapshot:
        if model == GRPC_DEFAULT_MODEL:
            visibility = ModelRouteVisibility.INTERNAL
        elif visibility is ModelRouteVisibility.INTERNAL:
            raise WorkflowConflictError(
                "只有 grpc-default 可以使用 internal 路由"
            )
        workflow = await self._workflow(workflow_id)
        if (
            model == GRPC_DEFAULT_MODEL
            and workflow.kind is not WorkflowKind.TRANSCRIPTION
        ):
            raise WorkflowConflictError(
                "grpc-default 只能指向转写工作流"
            )
        if self.current_or_none(workflow_id) is None:
            raise PipelineNotActiveError(
                "模型路由只能指向已激活工作流"
            )
        now = utc_now()
        async with self._write_lock:
            async with self._session_factory() as session:
                route = await session.get(ModelRoute, model)
                if route is None:
                    route = ModelRoute(
                        model=model,
                        workflow_id=workflow_id,
                        visibility=visibility,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(route)
                else:
                    route.workflow_id = workflow_id
                    route.visibility = visibility
                    route.updated_at = now
                await session.commit()
                await session.refresh(route)
                snapshot = ModelRouteSnapshot(
                    model=route.model,
                    workflow_id=route.workflow_id,
                    visibility=route.visibility,
                    updated_at=route.updated_at,
                )
                self._routes[model] = snapshot
                return snapshot

    async def delete_model_route(self, model: str) -> None:
        if model == GRPC_DEFAULT_MODEL:
            raise WorkflowConflictError("不能删除 grpc-default 路由")
        async with self._write_lock:
            async with self._session_factory() as session:
                route = await session.get(ModelRoute, model)
                if route is None:
                    raise ModelRouteNotFoundError(
                        f"模型路由不存在: {model}"
                    )
                await session.delete(route)
                await session.commit()
            self._routes.pop(model, None)

    async def delete_workflow(self, workflow_id: str) -> None:
        if workflow_id == DEFAULT_TRANSCRIPTION_WORKFLOW_ID:
            raise WorkflowConflictError("不能删除默认转写工作流")
        routes = [
            route
            for route in self._routes.values()
            if route.workflow_id == workflow_id
        ]
        if any(
            route.visibility is ModelRouteVisibility.INTERNAL
            for route in routes
        ):
            raise WorkflowConflictError(
                "工作流仍是 gRPC 默认入口，请先切换 grpc-default"
            )
        previous = self._active.get(workflow_id)
        async with self._write_lock:
            async with self._session_factory() as session:
                workflow = await session.get(Workflow, workflow_id)
                if workflow is None:
                    raise WorkflowNotFoundError(
                        f"工作流不存在: {workflow_id}"
                    )
                activation = await session.get(WorkflowActivation, workflow_id)
                if activation is not None:
                    await session.delete(activation)
                    await session.flush()
                await session.execute(
                    delete(ModelRoute).where(ModelRoute.workflow_id == workflow_id)
                )
                await session.execute(
                    delete(WorkflowRevision).where(
                        WorkflowRevision.workflow_id == workflow_id
                    )
                )
                await session.delete(workflow)
                await session.commit()
        self._active.pop(workflow_id, None)
        for route in routes:
            self._routes.pop(route.model, None)
        if previous is not None:
            active_keys = {
                (item.id, item.revision)
                for active in self._active.values()
                for item in active.plan.engine_snapshots
            }
            for item in previous.plan.engine_snapshots:
                if (item.id, item.revision) not in active_keys:
                    await self._engine_router.retire_snapshot(
                        item.id,
                        item.revision,
                    )

    async def asset_references(self, asset_id: str) -> tuple[str, ...]:
        references: list[str] = []
        async with self._session_factory() as session:
            result = await session.execute(select(Workflow))
            for workflow in result.scalars().all():
                if asset_id in definition_engine_ids(
                    workflow.draft_definition
                ):
                    references.append(f"{workflow.name} 草稿")
                active = self._active.get(workflow.id)
                if active is not None and asset_id in definition_engine_ids(
                    active.definition.model_dump(mode="json")
                ):
                    references.append(
                        f"{workflow.name} 激活修订 {active.revision}"
                    )
        return tuple(references)
