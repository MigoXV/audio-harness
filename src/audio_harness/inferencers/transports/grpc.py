from __future__ import annotations

import asyncio

import grpc

from audio_harness.configs import EngineEndpoint
from audio_harness.errors import (
    EngineDeadlineError,
    EngineUnavailableError,
)


def raise_grpc_error(
    error: grpc.aio.AioRpcError,
    inferencer_name: str,
) -> None:
    details = error.details() or error.code().name
    if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
        raise EngineDeadlineError(
            f"{inferencer_name} 请求超时: {details}"
        ) from error
    raise EngineUnavailableError(
        f"{inferencer_name} 请求失败({error.code().name}): {details}"
    ) from error


class GrpcTransport:
    def __init__(
        self,
        endpoint: EngineEndpoint,
        *,
        max_message_bytes: int,
    ) -> None:
        self.endpoint = endpoint
        options = (
            ("grpc.max_send_message_length", max_message_bytes),
            ("grpc.max_receive_message_length", max_message_bytes),
        )
        self.channel = grpc.aio.insecure_channel(
            endpoint.target,
            options=options,
        )

    async def ready(self) -> None:
        try:
            await asyncio.wait_for(
                self.channel.channel_ready(),
                timeout=self.endpoint.connect_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise EngineUnavailableError(
                f"连接推理能力超时: {self.endpoint.target}"
            ) from exc

    async def probe(self) -> None:
        await self.ready()

    async def close(self) -> None:
        await self.channel.close()
