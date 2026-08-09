from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from audio_harness.database.session import Database
from audio_harness.voice_agents.service import RealtimeRouteService
from audio_harness.workflow_settings import WorkflowSettingsStore
from audio_harness.workflow_types import ModelRouteVisibility
from audio_harness.workflows.service import ActiveAudioWorkflow, AudioWorkflowService


ComponentState = Literal[
    "ready",
    "needs_configuration",
    "unavailable",
]


@dataclass(frozen=True)
class RuntimeIssue:
    code: str
    severity: Literal["warning", "error"]
    scope: Literal["control_plane", "grpc", "realtime"]
    message: str
    resource_id: str | None = None


@dataclass(frozen=True)
class GrpcOverview:
    state: ComponentState
    workflow_id: str | None
    workflow_revision: int | None


@dataclass(frozen=True)
class RealtimeOverview:
    state: ComponentState
    public_routes: int
    internal_routes: int


@dataclass(frozen=True)
class RuntimeOverview:
    status: ComponentState
    control_plane: ComponentState
    grpc: GrpcOverview
    realtime: RealtimeOverview
    issues: tuple[RuntimeIssue, ...]

    @property
    def ready(self) -> bool:
        return self.control_plane == "ready" and self.grpc.state == "ready"


class ServingStatusService:
    """集中计算探针与控制台使用的协议就绪状态。"""

    def __init__(
        self,
        *,
        database: Database,
        workflows: AudioWorkflowService,
        workflow_settings: WorkflowSettingsStore,
        routes: RealtimeRouteService,
    ) -> None:
        self._database = database
        self._workflows = workflows
        self._workflow_settings = workflow_settings
        self._routes = routes

    async def snapshot(self) -> RuntimeOverview:
        issues: list[RuntimeIssue] = []
        try:
            await self._database.ping()
        except Exception:
            control_plane: ComponentState = "unavailable"
            issues.append(
                RuntimeIssue(
                    code="database_unavailable",
                    severity="error",
                    scope="control_plane",
                    message="SQLite 不可用",
                )
            )
        else:
            control_plane = "ready"

        settings = self._workflow_settings.current()
        active: ActiveAudioWorkflow | None = None
        if settings.grpc_audio_workflow_id is None:
            grpc_state: ComponentState = "needs_configuration"
            issues.append(
                RuntimeIssue(
                    code="grpc_workflow_unconfigured",
                    severity="warning",
                    scope="grpc",
                    message="未配置 gRPC 默认语音工作流",
                )
            )
        else:
            active = self._workflows.current_or_none(
                settings.grpc_audio_workflow_id
            )
            if active is None:
                grpc_state = "needs_configuration"
                issues.append(
                    RuntimeIssue(
                        code="grpc_workflow_unpublished",
                        severity="warning",
                        scope="grpc",
                        message="gRPC 默认语音工作流尚未发布",
                        resource_id=settings.grpc_audio_workflow_id,
                    )
                )
            else:
                grpc_state = "ready"

        public_routes = 0
        internal_routes = 0
        for route in self._routes.list():
            try:
                self._routes.resolve(route.model, allow_internal=True)
            except RuntimeError:
                issues.append(
                    RuntimeIssue(
                        code="realtime_route_target_inactive",
                        severity="warning",
                        scope="realtime",
                        message=f"Realtime 路由 {route.model} 的目标未发布",
                        resource_id=route.target_id,
                    )
                )
                continue
            if route.visibility is ModelRouteVisibility.PUBLIC:
                public_routes += 1
            else:
                internal_routes += 1

        if public_routes:
            realtime_state: ComponentState = "ready"
        else:
            realtime_state = "needs_configuration"
            issues.append(
                RuntimeIssue(
                    code="realtime_route_unconfigured",
                    severity="warning",
                    scope="realtime",
                    message="尚未配置公开 Realtime 模型路由",
                )
            )

        if control_plane == "unavailable":
            overall: ComponentState = "unavailable"
        elif issues:
            overall = "needs_configuration"
        else:
            overall = "ready"
        return RuntimeOverview(
            status=overall,
            control_plane=control_plane,
            grpc=GrpcOverview(
                state=grpc_state,
                workflow_id=(
                    active.id
                    if active is not None
                    else settings.grpc_audio_workflow_id
                ),
                workflow_revision=(active.revision if active is not None else None),
            ),
            realtime=RealtimeOverview(
                state=realtime_state,
                public_routes=public_routes,
                internal_routes=internal_routes,
            ),
            issues=tuple(issues),
        )
