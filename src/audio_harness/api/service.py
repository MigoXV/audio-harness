from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.api.schemas import EngineCreate, EnginePatch
from audio_harness.assets import EngineApi, EngineAssetData, EngineKind
from audio_harness.database.mappers import asset_data
from audio_harness.database.models import (
    EngineAsset,
    PipelineActivation,
    PipelineRevision,
    PipelineWorkingCopy,
    utc_now,
)
from audio_harness.engines.router import EngineRouter
from audio_harness.pipelines.service import definition_engine_ids


class CatalogError(Exception):
    code = "catalog_error"


class AssetNotFoundError(CatalogError):
    code = "engine_not_found"


class CatalogConflictError(CatalogError):
    code = "catalog_conflict"


class EngineCatalog:
    _RUNTIME_FIELDS = {
        "api",
        "protocol",
        "target",
        "connect_timeout_seconds",
        "request_timeout_seconds",
    }

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        router: EngineRouter,
    ) -> None:
        self._session_factory = session_factory
        self._router = router
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _validate_asset_shape(asset: EngineAssetData) -> None:
        expected = (
            EngineApi.UX_VAD
            if asset.kind is EngineKind.VAD
            else EngineApi.UX_SPEECH
        )
        if asset.api is not expected:
            raise CatalogConflictError(
                f"{asset.kind.value} 资产必须使用 {expected.value} 接口"
            )

    async def list_assets(
        self,
        *,
        limit: int,
        offset: int,
        kind: EngineKind | None,
        api: EngineApi | None,
        enabled: bool | None,
        query: str | None,
    ) -> tuple[list[EngineAssetData], int]:
        filters = []
        if kind is not None:
            filters.append(EngineAsset.kind == kind)
        if api is not None:
            filters.append(EngineAsset.api == api)
        if enabled is not None:
            filters.append(EngineAsset.enabled == enabled)
        if query:
            pattern = f"%{query.strip()}%"
            filters.append(
                or_(
                    EngineAsset.name.ilike(pattern),
                    EngineAsset.slug.ilike(pattern),
                    EngineAsset.target.ilike(pattern),
                )
            )

        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(EngineAsset).where(*filters)
            )
            result = await session.execute(
                select(EngineAsset)
                .where(*filters)
                .order_by(EngineAsset.created_at, EngineAsset.id)
                .limit(limit)
                .offset(offset)
            )
            return (
                [asset_data(model) for model in result.scalars().all()],
                int(total or 0),
            )

    async def get_asset(self, asset_id: str) -> EngineAssetData:
        async with self._session_factory() as session:
            model = await session.get(EngineAsset, asset_id)
            if model is None:
                raise AssetNotFoundError(f"引擎资产不存在: {asset_id}")
            return asset_data(model)

    async def create_asset(self, payload: EngineCreate) -> EngineAssetData:
        values = payload.model_dump()
        candidate = EngineAssetData(
            id="candidate",
            revision=1,
            created_at=None,
            updated_at=None,
            **values,
        )
        self._validate_asset_shape(candidate)
        async with self._write_lock:
            async with self._session_factory() as session:
                model = EngineAsset(
                    id=str(uuid4()),
                    revision=1,
                    **values,
                )
                session.add(model)
                try:
                    await session.commit()
                except IntegrityError as exc:
                    await session.rollback()
                    raise CatalogConflictError(
                        f"引擎 slug 已存在: {payload.slug}"
                    ) from exc
                await session.refresh(model)
                return asset_data(model)

    async def _pipeline_references(
        self,
        session: AsyncSession,
        asset_id: str,
    ) -> list[str]:
        references: list[str] = []
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
                references.append(f"激活修订 {revision.revision}")
        return references

    async def update_asset(
        self,
        asset_id: str,
        payload: EnginePatch,
    ) -> EngineAssetData:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            return await self.get_asset(asset_id)

        async with self._write_lock:
            async with self._session_factory() as session:
                model = await session.get(EngineAsset, asset_id)
                if model is None:
                    raise AssetNotFoundError(f"引擎资产不存在: {asset_id}")
                current = asset_data(model)
                candidate = replace(current, **changes)
                references = await self._pipeline_references(
                    session,
                    asset_id,
                )
                if references and not candidate.enabled:
                    raise CatalogConflictError(
                        "处理链正在引用该引擎，不能停用: "
                        + ", ".join(references)
                    )
                if references and candidate.api is not current.api:
                    raise CatalogConflictError(
                        "处理链正在引用该引擎，不能修改能力接口: "
                        + ", ".join(references)
                    )
                runtime_changed = bool(self._RUNTIME_FIELDS.intersection(changes))
                if runtime_changed:
                    candidate = replace(
                        candidate,
                        revision=current.revision + 1,
                    )
                self._validate_asset_shape(candidate)

                for key, value in changes.items():
                    setattr(model, key, value)
                if runtime_changed:
                    model.revision = candidate.revision
                model.updated_at = utc_now()
                try:
                    await session.commit()
                except IntegrityError as exc:
                    await session.rollback()
                    raise CatalogConflictError(
                        f"引擎 slug 已存在: {candidate.slug}"
                    ) from exc
                await session.refresh(model)

            active_reference = any(
                reference.startswith("激活修订")
                for reference in references
            )
            if runtime_changed and not active_reference:
                await self._router.retire(
                    asset_id,
                    before_revision=candidate.revision,
                )
            return asset_data(model)

    async def delete_asset(self, asset_id: str) -> None:
        async with self._write_lock:
            async with self._session_factory() as session:
                model = await session.get(EngineAsset, asset_id)
                if model is None:
                    raise AssetNotFoundError(f"引擎资产不存在: {asset_id}")
                references = await self._pipeline_references(
                    session,
                    asset_id,
                )
                if references:
                    raise CatalogConflictError(
                        "处理链正在引用该引擎，不能删除: "
                        + ", ".join(references)
                    )
                await session.delete(model)
                await session.commit()
            await self._router.retire(asset_id)
