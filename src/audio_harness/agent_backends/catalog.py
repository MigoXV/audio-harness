from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from time import perf_counter
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.agent_backends.base import AgentBackendGateway
from audio_harness.agent_backends.models import (
    AgentBackendAssetData,
    AgentBackendCapabilities,
    AgentBackendKind,
    AgentBackendProtocol,
    AgentBackendStatus,
)
from audio_harness.database.mappers import agent_backend_data
from audio_harness.database.models import (
    AgentBackendAsset,
    VoiceAgentActivation,
    VoiceAgentProfile,
    VoiceAgentRevision,
    utc_now,
)


def _definition_backend_id(definition: Mapping[str, Any]) -> str | None:
    binding = definition.get("agent_backend")
    if not isinstance(binding, Mapping):
        return None
    value = binding.get("backend_id")
    return str(value) if value else None


class AgentBackendCatalogError(Exception):
    code = "agent_backend_catalog_error"


class AgentBackendNotFoundError(AgentBackendCatalogError):
    code = "agent_backend_not_found"


class AgentBackendConflictError(AgentBackendCatalogError):
    code = "agent_backend_conflict"


class AgentBackendUnavailableError(AgentBackendCatalogError):
    code = "agent_backend_unavailable"


@dataclass(frozen=True)
class AgentBackendValidationResult:
    asset: AgentBackendAssetData
    outcome: str
    message: str | None = None
    latency_ms: float | None = None


class AgentBackendCatalog:
    _RUNTIME_FIELDS = {
        "kind",
        "protocol",
        "target",
        "connect_timeout_seconds",
        "request_timeout_seconds",
        "headers_env",
        "details",
    }

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: AgentBackendGateway,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _validate_asset_shape(asset: AgentBackendAssetData) -> None:
        if asset.kind is not AgentBackendKind.DSH:
            raise AgentBackendConflictError(
                f"尚不支持 Agent Backend kind: {asset.kind.value}"
            )
        if asset.protocol is not AgentBackendProtocol.DSH_WEB_V1:
            raise AgentBackendConflictError(
                f"DSH 必须使用 {AgentBackendProtocol.DSH_WEB_V1.value} 协议"
            )
        parsed = urlparse(asset.target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AgentBackendConflictError(
                "Agent Backend target 必须是 HTTP(S) base URL"
            )
        if parsed.query or parsed.fragment:
            raise AgentBackendConflictError(
                "Agent Backend target 不能包含 query 或 fragment"
            )

    async def list_assets(
        self,
        *,
        limit: int,
        offset: int,
        kind: AgentBackendKind | None,
        protocol: AgentBackendProtocol | None,
        enabled: bool | None,
        query: str | None,
    ) -> tuple[list[AgentBackendAssetData], int]:
        filters = []
        if kind is not None:
            filters.append(AgentBackendAsset.kind == kind)
        if protocol is not None:
            filters.append(AgentBackendAsset.protocol == protocol)
        if enabled is not None:
            filters.append(AgentBackendAsset.enabled == enabled)
        if query:
            pattern = f"%{query.strip()}%"
            filters.append(
                or_(
                    AgentBackendAsset.name.ilike(pattern),
                    AgentBackendAsset.slug.ilike(pattern),
                    AgentBackendAsset.target.ilike(pattern),
                )
            )
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(AgentBackendAsset)
                .where(*filters)
            )
            result = await session.execute(
                select(AgentBackendAsset)
                .where(*filters)
                .order_by(AgentBackendAsset.created_at, AgentBackendAsset.id)
                .limit(limit)
                .offset(offset)
            )
            return (
                [agent_backend_data(model) for model in result.scalars().all()],
                int(total or 0),
            )

    async def get_asset(self, asset_id: str) -> AgentBackendAssetData:
        async with self._session_factory() as session:
            model = await session.get(AgentBackendAsset, asset_id)
            if model is None:
                raise AgentBackendNotFoundError(
                    f"Agent Backend 不存在: {asset_id}"
                )
            return agent_backend_data(model)

    async def create_asset(
        self,
        payload: Mapping[str, Any],
    ) -> AgentBackendAssetData:
        values = dict(payload)
        values["status"] = (
            AgentBackendStatus.UNKNOWN
            if values["enabled"]
            else AgentBackendStatus.DISABLED
        )
        candidate = AgentBackendAssetData(
            id="candidate",
            revision=1,
            created_at=None,
            updated_at=None,
            status_basis=None,
            status_observed_at=None,
            last_success_at=None,
            probe_latency_ms=None,
            consecutive_failures=0,
            next_probe_at=utc_now() if values["enabled"] else None,
            **values,
        )
        self._validate_asset_shape(candidate)
        async with self._write_lock:
            async with self._session_factory() as session:
                model = AgentBackendAsset(
                    id=str(uuid4()),
                    revision=1,
                    next_probe_at=(utc_now() if values["enabled"] else None),
                    **values,
                )
                session.add(model)
                try:
                    await session.commit()
                except IntegrityError as exc:
                    await session.rollback()
                    raise AgentBackendConflictError(
                        f"Agent Backend slug 已存在: {values['slug']}"
                    ) from exc
                await session.refresh(model)
                return agent_backend_data(model)

    async def _references(
        self,
        session: AsyncSession,
        asset_id: str,
    ) -> list[str]:
        references: list[str] = []
        result = await session.execute(select(VoiceAgentProfile))
        for profile in result.scalars().all():
            if _definition_backend_id(profile.draft_definition) == asset_id:
                references.append(f"{profile.name} 语音智能体草稿")
            activation = await session.get(VoiceAgentActivation, profile.id)
            if activation is None or activation.active_revision_id is None:
                continue
            revision = await session.get(
                VoiceAgentRevision,
                activation.active_revision_id,
            )
            if (
                revision is not None
                and _definition_backend_id(revision.definition) == asset_id
            ):
                references.append(
                    f"{profile.name} 语音智能体激活修订 {revision.revision}"
                )
        return references

    async def update_asset(
        self,
        asset_id: str,
        payload: Mapping[str, Any],
    ) -> AgentBackendAssetData:
        changes = dict(payload)
        if not changes:
            return await self.get_asset(asset_id)
        async with self._write_lock:
            async with self._session_factory() as session:
                model = await session.get(AgentBackendAsset, asset_id)
                if model is None:
                    raise AgentBackendNotFoundError(
                        f"Agent Backend 不存在: {asset_id}"
                    )
                current = agent_backend_data(model)
                candidate = replace(current, **changes)
                references = await self._references(session, asset_id)
                if references and not candidate.enabled:
                    raise AgentBackendConflictError(
                        "语音智能体正在引用该 Agent Backend，不能停用: "
                        + ", ".join(references)
                    )
                if references and (
                    candidate.kind is not current.kind
                    or candidate.protocol is not current.protocol
                ):
                    raise AgentBackendConflictError(
                        "语音智能体正在引用该 Agent Backend，不能修改类型: "
                        + ", ".join(references)
                    )
                runtime_changed = bool(
                    self._RUNTIME_FIELDS.intersection(changes)
                )
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
                        model.status = AgentBackendStatus.UNKNOWN
                        model.next_probe_at = utc_now()
                    else:
                        model.status = AgentBackendStatus.DISABLED
                        model.next_probe_at = None
                if runtime_changed:
                    model.revision = candidate.revision
                    model.status = AgentBackendStatus.UNKNOWN
                    model.next_probe_at = utc_now()
                model.updated_at = utc_now()
                try:
                    await session.commit()
                except IntegrityError as exc:
                    await session.rollback()
                    raise AgentBackendConflictError(
                        f"Agent Backend slug 已存在: {candidate.slug}"
                    ) from exc
                await session.refresh(model)
            active_reference = any("激活修订" in item for item in references)
            if runtime_changed and not active_reference:
                await self._gateway.retire(
                    asset_id,
                    before_revision=candidate.revision,
                )
            return agent_backend_data(model)

    async def references(self, asset_id: str) -> tuple[str, ...]:
        async with self._session_factory() as session:
            if await session.get(AgentBackendAsset, asset_id) is None:
                raise AgentBackendNotFoundError(
                    f"Agent Backend 不存在: {asset_id}"
                )
            return tuple(await self._references(session, asset_id))

    async def delete_asset(self, asset_id: str) -> None:
        async with self._write_lock:
            async with self._session_factory() as session:
                model = await session.get(AgentBackendAsset, asset_id)
                if model is None:
                    raise AgentBackendNotFoundError(
                        f"Agent Backend 不存在: {asset_id}"
                    )
                references = await self._references(session, asset_id)
                if references:
                    raise AgentBackendConflictError(
                        "语音智能体正在引用该 Agent Backend，不能删除: "
                        + ", ".join(references)
                    )
                await session.delete(model)
                await session.commit()
            await self._gateway.retire(asset_id)

    async def retire_snapshot(self, asset_id: str, revision: int) -> None:
        await self._gateway.retire(
            asset_id,
            before_revision=revision + 1,
        )

    async def capabilities(
        self,
        asset_id: str,
    ) -> tuple[AgentBackendAssetData, AgentBackendCapabilities]:
        asset = await self.get_asset(asset_id)
        if not asset.enabled:
            raise AgentBackendConflictError(
                "已停用的 Agent Backend 不能发现能力"
            )
        try:
            value = await self._gateway.capabilities(asset.snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AgentBackendUnavailableError(str(exc)) from exc
        return asset, value

    @staticmethod
    def _error_message(exc: Exception) -> str:
        return (str(exc).strip() or exc.__class__.__name__)[:500]

    async def validate_asset(
        self,
        asset_id: str,
        *,
        automated: bool = False,
    ) -> AgentBackendValidationResult:
        asset = await self.get_asset(asset_id)
        if not asset.enabled:
            raise AgentBackendConflictError(
                "已停用的 Agent Backend 不能执行健康探测"
            )
        started = perf_counter()
        try:
            await self._gateway.probe(asset.snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = AgentBackendValidationResult(
                asset=asset,
                outcome="failed",
                message=self._error_message(exc),
                latency_ms=(perf_counter() - started) * 1000,
            )
        else:
            result = AgentBackendValidationResult(
                asset=asset,
                outcome="passed",
                latency_ms=(perf_counter() - started) * 1000,
            )
        now = utc_now()
        async with self._write_lock:
            async with self._session_factory() as session:
                model = await session.get(AgentBackendAsset, asset_id)
                if model is None or model.revision != asset.revision:
                    return replace(
                        result,
                        outcome="stale",
                        message="探测期间 Agent Backend 配置已变更，结果未写入",
                    )
                passed = result.outcome == "passed"
                model.status = (
                    AgentBackendStatus.HEALTHY
                    if passed
                    else AgentBackendStatus.UNHEALTHY
                )
                prefix = "后台" if automated else "人工"
                model.status_basis = (
                    f"{prefix}连通性校验通过"
                    if passed
                    else f"{prefix}连通性校验失败：{result.message}"
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
                select(AgentBackendAsset.id).where(
                    AgentBackendAsset.enabled.is_(True),
                    or_(
                        AgentBackendAsset.next_probe_at.is_(None),
                        AgentBackendAsset.next_probe_at <= now,
                    ),
                )
            )
            return tuple(result.scalars().all())
