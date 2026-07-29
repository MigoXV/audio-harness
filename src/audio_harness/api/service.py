from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from audio_harness.api.schemas import EngineCreate, EnginePatch
from audio_harness.assets import (
    EngineApi,
    EngineAssetData,
    EngineBindingData,
    EngineKind,
    EngineRole,
)
from audio_harness.database.mappers import asset_data, binding_data
from audio_harness.database.models import (
    EngineAsset,
    EngineBinding,
    utc_now,
)
from audio_harness.engines.router import EngineRouter


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

    async def _bound_roles(
        self,
        session: AsyncSession,
        asset_id: str,
    ) -> list[EngineRole]:
        result = await session.scalars(
            select(EngineBinding.role).where(
                EngineBinding.engine_id == asset_id
            )
        )
        return list(result.all())

    @staticmethod
    def _validate_roles(
        asset: EngineAssetData,
        roles: list[EngineRole],
    ) -> None:
        if roles and not asset.enabled:
            raise CatalogConflictError("已绑定引擎不能直接禁用，请先解除绑定")
        incompatible = [role.value for role in roles if asset.api is not role.required_api]
        if incompatible:
            raise CatalogConflictError(
                "引擎接口与已绑定角色不兼容: " + ", ".join(incompatible)
            )

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
                roles = await self._bound_roles(session, asset_id)
                current = asset_data(model)
                candidate = replace(current, **changes)
                runtime_changed = bool(self._RUNTIME_FIELDS.intersection(changes))
                if runtime_changed:
                    candidate = replace(
                        candidate,
                        revision=current.revision + 1,
                    )
                self._validate_asset_shape(candidate)
                self._validate_roles(candidate, roles)
                if runtime_changed and roles:
                    await self._router.prepare(candidate.snapshot())

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

            if runtime_changed:
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
                roles = await self._bound_roles(session, asset_id)
                if roles:
                    raise CatalogConflictError(
                        "已绑定引擎不能删除: "
                        + ", ".join(role.value for role in roles)
                    )
                await session.delete(model)
                await session.commit()
            await self._router.retire(asset_id)

    async def list_bindings(self) -> list[EngineBindingData]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(EngineBinding).options(selectinload(EngineBinding.engine))
            )
            existing = {
                model.role: binding_data(model)
                for model in result.scalars().all()
            }
        return [
            existing.get(
                role,
                EngineBindingData(
                    role=role,
                    engine=None,
                    generation=0,
                    updated_at=None,
                ),
            )
            for role in EngineRole
        ]

    async def get_binding(self, role: EngineRole) -> EngineBindingData:
        return next(
            binding
            for binding in await self.list_bindings()
            if binding.role is role
        )

    async def bind(
        self,
        role: EngineRole,
        asset_id: str,
    ) -> EngineBindingData:
        old_asset_id: str | None = None
        async with self._write_lock:
            async with self._session_factory() as session:
                asset = await session.get(EngineAsset, asset_id)
                if asset is None:
                    raise AssetNotFoundError(f"引擎资产不存在: {asset_id}")
                data = asset_data(asset)
                if not data.enabled:
                    raise CatalogConflictError("已停用引擎不能绑定")
                if data.api is not role.required_api:
                    raise CatalogConflictError(
                        f"{role.value} 需要 {role.required_api.value}，"
                        f"实际为 {data.api.value}"
                    )

                await self._router.prepare(data.snapshot())
                binding = await session.get(EngineBinding, role)
                if binding is None:
                    binding = EngineBinding(
                        role=role,
                        engine_id=asset_id,
                        generation=1,
                    )
                    session.add(binding)
                else:
                    old_asset_id = binding.engine_id
                    binding.engine_id = asset_id
                    binding.generation += 1
                    binding.updated_at = utc_now()
                await session.commit()

            if old_asset_id is not None and old_asset_id != asset_id:
                async with self._session_factory() as session:
                    remaining = await session.scalar(
                        select(func.count())
                        .select_from(EngineBinding)
                        .where(EngineBinding.engine_id == old_asset_id)
                    )
                if not remaining:
                    await self._router.retire(old_asset_id)
        return await self.get_binding(role)

    async def unbind(self, role: EngineRole) -> None:
        old_asset_id: str | None = None
        async with self._write_lock:
            async with self._session_factory() as session:
                binding = await session.get(EngineBinding, role)
                if binding is None:
                    return
                old_asset_id = binding.engine_id
                await session.delete(binding)
                await session.commit()
            async with self._session_factory() as session:
                remaining = await session.scalar(
                    select(func.count())
                    .select_from(EngineBinding)
                    .where(EngineBinding.engine_id == old_asset_id)
                )
            if not remaining and old_asset_id is not None:
                await self._router.retire(old_asset_id)
