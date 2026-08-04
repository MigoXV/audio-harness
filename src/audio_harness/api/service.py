from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from time import perf_counter
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.api.schemas import EngineCreate, EnginePatch
from audio_harness.assets import (
    EngineApi,
    EngineAssetData,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.database.mappers import asset_data
from audio_harness.database.models import (
    EngineAsset,
    Workflow,
    WorkflowActivation,
    WorkflowRevision,
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


@dataclass(frozen=True)
class EngineValidationResult:
    asset: EngineAssetData
    outcome: str
    message: str | None = None
    latency_ms: float | None = None


class EngineCatalog:
    _RUNTIME_FIELDS = {
        "api",
        "protocol",
        "target",
        "connect_timeout_seconds",
        "request_timeout_seconds",
        "credential_env",
        "api_key",
        "details",
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
        expected = {
            EngineKind.ASR: EngineApi.UX_SPEECH,
            EngineKind.VAD: EngineApi.UX_VAD,
            EngineKind.LLM: EngineApi.RESPONSES,
            EngineKind.TTS: EngineApi.QWEN_TTS_DUPLEX,
        }[asset.kind]
        if asset.api is not expected:
            raise CatalogConflictError(
                f"{asset.kind.value} 资产必须使用 {expected.value} 接口"
            )
        expected_protocol = (
            EngineProtocol.HTTPS
            if asset.api is EngineApi.RESPONSES
            else EngineProtocol.GRPC
        )
        if asset.protocol is not expected_protocol:
            raise CatalogConflictError(
                f"{asset.api.value} 资产必须使用 "
                f"{expected_protocol.value} 协议"
            )
        if asset.api is EngineApi.RESPONSES:
            if not asset.api_key and not asset.credential_env:
                raise CatalogConflictError(
                    "Responses 资产必须配置 API Key"
                )
            if not asset.target.startswith(("https://", "http://")):
                raise CatalogConflictError(
                    "Responses target 必须是 HTTP(S) API base URL"
                )
        elif asset.credential_env is not None or asset.api_key is not None:
            raise CatalogConflictError(
                "只有 Responses 资产可以配置模型 API Key"
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

    @staticmethod
    def _validation_message(exc: Exception) -> str:
        message = str(exc).strip() or exc.__class__.__name__
        return message[:500]

    async def validate_enabled_assets(
        self,
        *,
        automated: bool = False,
    ) -> list[EngineValidationResult]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(EngineAsset)
                .where(EngineAsset.enabled.is_(True))
                .order_by(EngineAsset.created_at, EngineAsset.id)
            )
            assets = [asset_data(model) for model in result.scalars().all()]

        semaphore = asyncio.Semaphore(8)

        async def validate(asset: EngineAssetData) -> EngineValidationResult:
            started = perf_counter()
            try:
                async with semaphore:
                    await self._router.probe(asset.snapshot())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return EngineValidationResult(
                    asset=asset,
                    outcome="failed",
                    message=self._validation_message(exc),
                    latency_ms=(perf_counter() - started) * 1000,
                )
            return EngineValidationResult(
                asset=asset,
                outcome="passed",
                latency_ms=(perf_counter() - started) * 1000,
            )

        results = list(await asyncio.gather(*(validate(asset) for asset in assets)))
        if not results:
            return results

        now = utc_now()
        persisted: list[EngineValidationResult] = []
        async with self._write_lock:
            async with self._session_factory() as session:
                for item in results:
                    model = await session.get(EngineAsset, item.asset.id)
                    if (
                        model is None
                        or not model.enabled
                        or model.revision != item.asset.revision
                    ):
                        persisted.append(
                            EngineValidationResult(
                                asset=item.asset,
                                outcome="stale",
                                message="校验期间引擎配置已变更，结果未写入",
                            )
                        )
                        continue
                    passed = item.outcome == "passed"
                    model.status = (
                        EngineStatus.HEALTHY
                        if passed
                        else EngineStatus.UNHEALTHY
                    )
                    model.status_basis = (
                        ("后台" if automated else "人工") + "连通性校验通过"
                        if passed
                        else ("后台" if automated else "人工")
                        + f"连通性校验失败：{item.message}"
                    )
                    model.status_observed_at = now
                    model.probe_latency_ms = item.latency_ms
                    if passed:
                        model.last_success_at = now
                        model.consecutive_failures = 0
                        delay_seconds = 60
                    else:
                        model.consecutive_failures = (
                            model.consecutive_failures or 0
                        ) + 1
                        delay_seconds = min(
                            300,
                            60 * (2 ** (model.consecutive_failures - 1)),
                        )
                    model.next_probe_at = now + timedelta(
                        seconds=delay_seconds
                    )
                    model.updated_at = now
                    persisted.append(item)
                await session.commit()
        return persisted

    async def validate_asset(
        self,
        asset_id: str,
        *,
        automated: bool = False,
    ) -> EngineValidationResult:
        asset = await self.get_asset(asset_id)
        if not asset.enabled:
            raise CatalogConflictError("已停用引擎不能执行健康探测")
        started = perf_counter()
        try:
            await self._router.probe(asset.snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = EngineValidationResult(
                asset=asset,
                outcome="failed",
                message=self._validation_message(exc),
                latency_ms=(perf_counter() - started) * 1000,
            )
        else:
            result = EngineValidationResult(
                asset=asset,
                outcome="passed",
                latency_ms=(perf_counter() - started) * 1000,
            )
        now = utc_now()
        async with self._write_lock:
            async with self._session_factory() as session:
                model = await session.get(EngineAsset, asset_id)
                if model is None or model.revision != asset.revision:
                    return replace(
                        result,
                        outcome="stale",
                        message="探测期间引擎配置已变更，结果未写入",
                    )
                passed = result.outcome == "passed"
                model.status = (
                    EngineStatus.HEALTHY if passed else EngineStatus.UNHEALTHY
                )
                model.status_basis = (
                    ("后台" if automated else "人工") + "连通性校验通过"
                    if passed
                    else ("后台" if automated else "人工")
                    + f"连通性校验失败：{result.message}"
                )
                model.status_observed_at = now
                model.probe_latency_ms = result.latency_ms
                if passed:
                    model.last_success_at = now
                    model.consecutive_failures = 0
                    delay_seconds = 60
                else:
                    model.consecutive_failures = (
                        model.consecutive_failures or 0
                    ) + 1
                    delay_seconds = min(
                        300,
                        60 * (2 ** (model.consecutive_failures - 1)),
                    )
                model.next_probe_at = now + timedelta(seconds=delay_seconds)
                model.updated_at = now
                await session.commit()
        return result

    async def due_asset_ids(self) -> tuple[str, ...]:
        now = utc_now()
        async with self._session_factory() as session:
            result = await session.execute(
                select(EngineAsset.id).where(
                    EngineAsset.enabled.is_(True),
                    or_(
                        EngineAsset.next_probe_at.is_(None),
                        EngineAsset.next_probe_at <= now,
                    ),
                )
            )
            return tuple(result.scalars().all())

    async def create_asset(self, payload: EngineCreate) -> EngineAssetData:
        values = payload.model_dump()
        values["status"] = (
            EngineStatus.UNKNOWN if payload.enabled else EngineStatus.DISABLED
        )
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
        result = await session.execute(select(Workflow))
        for workflow in result.scalars().all():
            if asset_id in definition_engine_ids(workflow.draft_definition):
                references.append(f"{workflow.name} 草稿")
            activation = await session.get(WorkflowActivation, workflow.id)
            if activation is None or activation.active_revision_id is None:
                continue
            revision = await session.get(
                WorkflowRevision,
                activation.active_revision_id,
            )
            if (
                revision is not None
                and asset_id in definition_engine_ids(revision.definition)
            ):
                references.append(
                    f"{workflow.name} 激活修订 {revision.revision}"
                )
        return references

    async def update_asset(
        self,
        asset_id: str,
        payload: EnginePatch,
    ) -> EngineAssetData:
        changes = payload.model_dump(exclude_unset=True)
        for derived in ("status", "status_basis", "status_observed_at"):
            changes.pop(derived, None)
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
                if "enabled" in changes:
                    if candidate.enabled:
                        model.status = EngineStatus.UNKNOWN
                        model.next_probe_at = utc_now()
                    else:
                        model.status = EngineStatus.DISABLED
                        model.next_probe_at = None
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

            active_reference = any("激活修订" in item for item in references)
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
