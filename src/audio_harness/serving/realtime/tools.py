from __future__ import annotations

import inspect
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from audio_harness.voice_agents.models import McpServerConfig

ToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def response_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": True,
        }


class ToolRegistry:
    """进程内服务工具注册表，供业务代码在 Host 启动前注入。"""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"服务工具已经注册: {name}")
        self._tools[name] = RegisteredTool(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
        )

    def definitions(self, names: list[str]) -> list[dict[str, Any]]:
        missing = [name for name in names if name not in self._tools]
        if missing:
            raise RuntimeError("服务工具未注册: " + ", ".join(missing))
        return [self._tools[name].response_definition() for name in names]

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise RuntimeError(f"服务工具未注册: {name}")
        value = tool.handler(arguments)
        return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class McpTool:
    public_name: str
    server_label: str
    remote_name: str
    description: str
    parameters: dict[str, Any]
    require_approval: str | dict[str, Any]

    def approval_required(self) -> bool:
        value = self.require_approval
        if value == "never":
            return False
        if value == "always" or not isinstance(value, dict):
            return True

        def matches(rule: Any) -> bool:
            if not isinstance(rule, dict):
                return False
            names = rule.get("tool_names")
            return names is None or self.remote_name in names

        if matches(value.get("never")):
            return False
        if matches(value.get("always")):
            return True
        return True

    def response_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.public_name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": False,
        }


class McpToolPool:
    def __init__(
        self,
        servers: list[McpServerConfig],
        *,
        memory_headers: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._servers = {item.server_label: item for item in servers}
        self._tools: dict[str, McpTool] = {}
        self._memory_headers = memory_headers or {}

    def _headers(self, config: McpServerConfig) -> dict[str, str]:
        headers = dict(self._memory_headers.get(config.server_label, {}))
        for name, variable in config.headers_env.items():
            value = os.environ.get(variable)
            if not value:
                raise RuntimeError(
                    f"MCP 凭据环境变量未设置: {variable}"
                )
            headers[name] = value
        return headers

    @asynccontextmanager
    async def _session(self, config: McpServerConfig):
        headers = self._headers(config)
        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(
                httpx.AsyncClient(headers=headers)
            )
            streams = await stack.enter_async_context(
                streamable_http_client(
                    config.server_url,
                    http_client=client,
                )
            )
            session = await stack.enter_async_context(
                ClientSession(streams[0], streams[1])
            )
            await session.initialize()
        except Exception as stream_error:
            await stack.aclose()
            stack = AsyncExitStack()
            try:
                streams = await stack.enter_async_context(
                    sse_client(
                        config.server_url,
                        headers=headers,
                    )
                )
                session = await stack.enter_async_context(
                    ClientSession(streams[0], streams[1])
                )
                await session.initialize()
            except Exception:
                await stack.aclose()
                raise RuntimeError(
                    f"MCP {config.server_label} 连接失败"
                    "（Streamable HTTP 与 SSE）"
                ) from stream_error
        try:
            yield session
        finally:
            await stack.aclose()

    async def definitions(self) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for config in self._servers.values():
            async with self._session(config) as session:
                cursor: str | None = None
                while True:
                    result = await session.list_tools(cursor=cursor)
                    for remote in result.tools:
                        if (
                            config.allowed_tools is not None
                            and remote.name not in config.allowed_tools
                        ):
                            continue
                        public_name = (
                            f"mcp__{config.server_label}__{remote.name}"
                        )
                        tool = McpTool(
                            public_name=public_name,
                            server_label=config.server_label,
                            remote_name=remote.name,
                            description=remote.description or remote.name,
                            parameters=remote.inputSchema,
                            require_approval=config.require_approval,
                        )
                        self._tools[public_name] = tool
                        definitions.append(tool.response_definition())
                    cursor = result.nextCursor
                    if not cursor:
                        break
        return definitions

    def tool(self, public_name: str) -> McpTool | None:
        return self._tools.get(public_name)

    async def call(self, public_name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(public_name)
        if tool is None:
            raise RuntimeError(f"MCP 工具不存在: {public_name}")
        config = self._servers[tool.server_label]
        async with self._session(config) as session:
            result = await session.call_tool(tool.remote_name, arguments)
        if hasattr(result, "model_dump"):
            value = result.model_dump(mode="json", exclude_none=True)
        else:
            value = result
        return json.dumps(value, ensure_ascii=False, default=str)


def json_tool_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)
