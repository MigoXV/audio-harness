from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.database.models import RealtimeRoute, utc_now
from audio_harness.realtime_routes.models import (
    ModelRouteVisibility,
    RealtimeRouteSnapshot,
    RealtimeRouteTargetType,
)
from audio_harness.voice_agents.service import ActiveVoiceAgent, VoiceAgentService
from audio_harness.workflows.service import ActiveAudioWorkflow, AudioWorkflowService


class RealtimeRouteNotFoundError(RuntimeError):
    pass


class RealtimeRouteConflictError(RuntimeError):
    def __init__(self, message: str, *, current: RealtimeRouteSnapshot) -> None:
        super().__init__(message)
        self.current = current


RealtimeTarget = ActiveAudioWorkflow | ActiveVoiceAgent


class RealtimeRouteService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        audio_workflows: AudioWorkflowService,
        voice_agents: VoiceAgentService,
    ) -> None:
        self._session_factory = session_factory
        self._audio_workflows = audio_workflows
        self._voice_agents = voice_agents
        self._routes: dict[str, RealtimeRouteSnapshot] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._session_factory() as session:
            models = (await session.execute(select(RealtimeRoute))).scalars().all()
        self._routes = {
            model.model: RealtimeRouteSnapshot(
                model=model.model,
                target_type=model.target_type,
                target_id=model.target_id,
                visibility=model.visibility,
                updated_at=model.updated_at,
            )
            for model in models
        }

    def list(self) -> tuple[RealtimeRouteSnapshot, ...]:
        return tuple(self._routes[key] for key in sorted(self._routes))

    def get(self, model: str) -> RealtimeRouteSnapshot:
        try:
            return self._routes[model]
        except KeyError as exc:
            raise RealtimeRouteNotFoundError(
                f"Realtime 模型路由不存在: {model}"
            ) from exc

    def resolve(self, model: str, *, allow_internal: bool = False) -> RealtimeTarget:
        route = self._routes.get(model)
        if route is None or (
            route.visibility is ModelRouteVisibility.INTERNAL and not allow_internal
        ):
            raise RealtimeRouteNotFoundError(f"Realtime 模型路由不存在: {model}")
        if route.target_type is RealtimeRouteTargetType.AUDIO_WORKFLOW:
            return self._audio_workflows.current(route.target_id)
        return self._voice_agents.current(route.target_id)

    async def upsert(
        self,
        *,
        model: str,
        target_type: RealtimeRouteTargetType,
        target_id: str,
        visibility: ModelRouteVisibility,
        replace_existing: bool = False,
    ) -> RealtimeRouteSnapshot:
        if target_type is RealtimeRouteTargetType.AUDIO_WORKFLOW:
            self._audio_workflows.current(target_id)
        else:
            self._voice_agents.current(target_id)
        now = utc_now()
        async with self._lock:
            async with self._session_factory() as session:
                route = await session.get(RealtimeRoute, model)
                if route is None:
                    route = RealtimeRoute(
                        model=model,
                        target_type=target_type,
                        target_id=target_id,
                        visibility=visibility,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(route)
                else:
                    current = RealtimeRouteSnapshot(
                        model=route.model,
                        target_type=route.target_type,
                        target_id=route.target_id,
                        visibility=route.visibility,
                        updated_at=route.updated_at,
                    )
                    target_changed = (
                        route.target_type != target_type
                        or route.target_id != target_id
                    )
                    if target_changed and not replace_existing:
                        raise RealtimeRouteConflictError(
                            f"Realtime 模型 {model} 已指向其他目标",
                            current=current,
                        )
                    route.target_type = target_type
                    route.target_id = target_id
                    route.visibility = visibility
                    route.updated_at = now
                await session.commit()
            snapshot = RealtimeRouteSnapshot(
                model=model,
                target_type=target_type,
                target_id=target_id,
                visibility=visibility,
                updated_at=now,
            )
            self._routes[model] = snapshot
            return snapshot

    async def delete(self, model: str) -> None:
        async with self._lock:
            async with self._session_factory() as session:
                route = await session.get(RealtimeRoute, model)
                if route is None:
                    raise RealtimeRouteNotFoundError(
                        f"Realtime 模型路由不存在: {model}"
                    )
                await session.delete(route)
                await session.commit()
            self._routes.pop(model, None)
