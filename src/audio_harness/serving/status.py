from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from audio_harness.database.session import Database
from audio_harness.serving.model_catalog import RealtimeModelCatalog


ComponentState = Literal[
    "ready",
    "needs_configuration",
    "unavailable",
]


@dataclass(frozen=True)
class RuntimeIssue:
    code: str
    severity: Literal["warning", "error"]
    scope: Literal["control_plane", "realtime"]
    message: str
    resource_id: str | None = None


@dataclass(frozen=True)
class RealtimeOverview:
    state: ComponentState
    public_routes: int
    internal_routes: int


@dataclass(frozen=True)
class RuntimeOverview:
    status: ComponentState
    control_plane: ComponentState
    realtime: RealtimeOverview
    issues: tuple[RuntimeIssue, ...]

    @property
    def ready(self) -> bool:
        return self.control_plane == "ready"


class ServingStatusService:
    """集中计算探针与控制台使用的协议就绪状态。"""

    def __init__(
        self,
        *,
        database: Database,
        models: RealtimeModelCatalog,
    ) -> None:
        self._database = database
        self._models = models

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

        public_routes = len(await self._models.list_models())
        internal_routes = 0

        if public_routes:
            realtime_state: ComponentState = "ready"
        else:
            realtime_state = "needs_configuration"
            issues.append(
                RuntimeIssue(
                    code="realtime_model_unconfigured",
                    severity="warning",
                    scope="realtime",
                    message="尚未发布可公开访问的 Realtime 模型",
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
            realtime=RealtimeOverview(
                state=realtime_state,
                public_routes=public_routes,
                internal_routes=internal_routes,
            ),
            issues=tuple(issues),
        )
