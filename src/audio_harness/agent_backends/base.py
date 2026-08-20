from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Literal

from audio_harness.agent_backends.models import (
    AgentBackendCapabilities,
    AgentBackendEvent,
    AgentBackendSnapshot,
)


class AgentBackendSession(ABC):
    @property
    @abstractmethod
    def session_id(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def prompt(
        self,
        text: str,
        *,
        mode: Literal["queue", "steer"] = "queue",
        client_time_zone: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def respond_approval(
        self,
        request_id: str,
        approval_id: str,
        *,
        approved: bool,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def respond_question(
        self,
        request_id: str,
        answers: Sequence[Mapping[str, Any]] | None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def events(self) -> AsyncIterator[AgentBackendEvent]:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


class AgentBackendGateway(ABC):
    """控制面只依赖该边界；具体 wire adapter 由组合根注入。"""

    @abstractmethod
    async def capabilities(
        self,
        snapshot: AgentBackendSnapshot,
    ) -> AgentBackendCapabilities:
        raise NotImplementedError

    async def probe(self, snapshot: AgentBackendSnapshot) -> None:
        await self.capabilities(snapshot)

    @abstractmethod
    async def open_session(
        self,
        snapshot: AgentBackendSnapshot,
        *,
        cwd: str,
        agent_preset: str,
    ) -> AgentBackendSession:
        raise NotImplementedError

    @abstractmethod
    async def retire(
        self,
        backend_id: str,
        *,
        before_revision: int | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


class UnavailableAgentBackendGateway(AgentBackendGateway):
    async def capabilities(
        self,
        snapshot: AgentBackendSnapshot,
    ) -> AgentBackendCapabilities:
        raise RuntimeError(
            f"尚未安装 {snapshot.protocol.value} Agent Backend 适配器"
        )

    async def open_session(
        self,
        snapshot: AgentBackendSnapshot,
        *,
        cwd: str,
        agent_preset: str,
    ) -> AgentBackendSession:
        raise RuntimeError(
            f"尚未安装 {snapshot.protocol.value} Agent Backend 适配器"
        )

    async def retire(
        self,
        backend_id: str,
        *,
        before_revision: int | None = None,
    ) -> None:
        return None

    async def close(self) -> None:
        return None
