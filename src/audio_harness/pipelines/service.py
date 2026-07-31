from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.database.mappers import asset_data
from audio_harness.database.models import (
    EngineAsset,
    PipelineActivation,
    PipelineRevision,
    PipelineWorkingCopy,
    utc_now,
)
from audio_harness.engines.router import EngineRouter
from audio_harness.pipelines.dag import (
    CompiledPipelinePlan,
    NodeTypeRegistry,
    PhysicalVadNodeConfig,
    PipelineDefinition,
    compile_pipeline,
    compiled_plan_document,
    restore_compiled_plan,
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


@dataclass(frozen=True)
class PipelineDraftSnapshot:
    definition: PipelineDefinition
    revision: int
    updated_at: datetime


@dataclass(frozen=True)
class ActivePipelineSnapshot:
    definition: PipelineDefinition
    plan: CompiledPipelinePlan
    revision: int
    activated_at: datetime


def definition_engine_ids(definition: dict[str, Any]) -> set[str]:
    return {
        str(node["engine_id"])
        for node in definition.get("nodes", [])
        if isinstance(node, dict) and node.get("engine_id")
    }


class PipelineStore:
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
        self._current: ActivePipelineSnapshot | None = None

    async def initialize(self) -> None:
        async with self._session_factory() as session:
            activation = await session.get(PipelineActivation, 1)
            if activation is None:
                raise RuntimeError("处理链数据库迁移尚未初始化")
            if activation.active_revision is None:
                self._current = None
                return
            revision = await session.get(
                PipelineRevision,
                activation.active_revision,
            )
            if revision is None:
                raise RuntimeError("当前激活处理链修订不存在")
            definition = PipelineDefinition.model_validate(
                revision.definition
            )
            plan = restore_compiled_plan(
                definition,
                revision.compiled_plan,
                registry=self.registry,
            )
            self._current = ActivePipelineSnapshot(
                definition=definition,
                plan=plan,
                revision=revision.revision,
                activated_at=revision.activated_at,
            )

    def current(self) -> ActivePipelineSnapshot:
        if self._current is None:
            raise PipelineNotActiveError("当前没有已激活的处理链")
        return self._current

    def current_or_none(self) -> ActivePipelineSnapshot | None:
        return self._current

    def physical_vad_config(self) -> PhysicalVadNodeConfig:
        active = self.current()
        node = next(
            (
                item
                for item in active.plan.nodes.values()
                if item.node_type.type == "physical_vad"
            ),
            None,
        )
        if node is None or not isinstance(
            node.config,
            PhysicalVadNodeConfig,
        ):
            raise PipelineNotActiveError(
                "当前处理链缺少物理 VAD 配置"
            )
        return node.config

    async def get_draft(self) -> PipelineDraftSnapshot:
        async with self._session_factory() as session:
            working = await session.get(PipelineWorkingCopy, 1)
            if working is None:
                raise RuntimeError("处理链工作副本不存在")
            return PipelineDraftSnapshot(
                definition=PipelineDefinition.model_validate(
                    working.definition
                ),
                revision=working.revision,
                updated_at=working.updated_at,
            )

    async def update_draft(
        self,
        *,
        definition: PipelineDefinition,
        expected_revision: int,
    ) -> PipelineDraftSnapshot:
        async with self._write_lock:
            async with self._session_factory() as session:
                working = await session.get(PipelineWorkingCopy, 1)
                if working is None:
                    raise RuntimeError("处理链工作副本不存在")
                if working.revision != expected_revision:
                    raise PipelineRevisionConflictError(
                        "处理链工作副本已经被其他操作更新"
                    )
                working.definition = definition.model_dump(mode="json")
                working.revision += 1
                working.updated_at = utc_now()
                await session.commit()
                await session.refresh(working)
                return PipelineDraftSnapshot(
                    definition=definition,
                    revision=working.revision,
                    updated_at=working.updated_at,
                )

    async def _assets(self) -> dict[str, Any]:
        async with self._session_factory() as session:
            result = await session.execute(select(EngineAsset))
            return {
                model.id: asset_data(model)
                for model in result.scalars().all()
            }

    async def compile(
        self,
        definition: PipelineDefinition,
    ) -> CompiledPipelinePlan:
        return compile_pipeline(
            definition,
            registry=self.registry,
            assets=await self._assets(),
        )

    async def validate_draft(self) -> CompiledPipelinePlan:
        draft = await self.get_draft()
        return await self.compile(draft.definition)

    async def activate(
        self,
        *,
        expected_revision: int,
    ) -> ActivePipelineSnapshot:
        async with self._write_lock:
            draft = await self.get_draft()
            if draft.revision != expected_revision:
                raise PipelineRevisionConflictError(
                    "处理链工作副本已经被其他操作更新"
                )
            plan = await self.compile(draft.definition)
            try:
                await self._engine_router.prepare_all(
                    plan.engine_snapshots
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise PipelineActivationError(
                    f"处理链引擎准备失败: {exc}"
                ) from exc

            now = utc_now()
            async with self._session_factory() as session:
                working = await session.get(PipelineWorkingCopy, 1)
                if (
                    working is None
                    or working.revision != expected_revision
                ):
                    raise PipelineRevisionConflictError(
                        "处理链工作副本已经被其他操作更新"
                    )
                for snapshot in plan.engine_snapshots:
                    asset = await session.get(EngineAsset, snapshot.id)
                    if (
                        asset is None
                        or asset.revision != snapshot.revision
                        or not asset.enabled
                    ):
                        raise PipelineRevisionConflictError(
                            "处理链引用的引擎在验证期间发生变化"
                        )

                latest = await session.scalar(
                    select(func.max(PipelineRevision.revision))
                )
                revision_number = int(latest or 0) + 1
                model = PipelineRevision(
                    revision=revision_number,
                    definition=draft.definition.model_dump(mode="json"),
                    compiled_plan=compiled_plan_document(plan),
                    created_at=now,
                    activated_at=now,
                )
                session.add(model)
                activation = await session.get(PipelineActivation, 1)
                if activation is None:
                    raise RuntimeError("处理链激活记录不存在")
                activation.active_revision = revision_number
                activation.updated_at = now
                await session.commit()

            previous = self._current
            snapshot = ActivePipelineSnapshot(
                definition=draft.definition,
                plan=plan,
                revision=revision_number,
                activated_at=now,
            )
            self._current = snapshot
            if previous is not None:
                active_keys = {
                    (item.id, item.revision)
                    for item in snapshot.plan.engine_snapshots
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
    ) -> ActivePipelineSnapshot:
        async with self._session_factory() as session:
            revision = await session.get(
                PipelineRevision,
                revision_number,
            )
            if revision is None:
                raise PipelineRevisionNotFoundError(
                    f"处理链修订不存在: {revision_number}"
                )
            definition = PipelineDefinition.model_validate(
                revision.definition
            )
            return ActivePipelineSnapshot(
                definition=definition,
                plan=restore_compiled_plan(
                    definition,
                    revision.compiled_plan,
                    registry=self.registry,
                ),
                revision=revision.revision,
                activated_at=revision.activated_at,
            )

    async def asset_references(self, asset_id: str) -> tuple[str, ...]:
        references: list[str] = []
        async with self._session_factory() as session:
            working = await session.get(PipelineWorkingCopy, 1)
            if (
                working is not None
                and asset_id in definition_engine_ids(working.definition)
            ):
                references.append("工作副本")
            activation = await session.get(PipelineActivation, 1)
            if (
                activation is not None
                and activation.active_revision is not None
            ):
                revision = await session.get(
                    PipelineRevision,
                    activation.active_revision,
                )
                if (
                    revision is not None
                    and asset_id
                    in definition_engine_ids(revision.definition)
                ):
                    references.append(
                        f"激活修订 {revision.revision}"
                    )
        return tuple(references)
