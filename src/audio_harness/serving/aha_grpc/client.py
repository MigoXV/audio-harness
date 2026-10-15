"""Small async client around the generated, cross-language gRPC contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import grpc

from audio_harness.protos.aha_grpc.v1 import aha_realtime_pb2 as pb
from audio_harness.protos.aha_grpc.v1 import aha_realtime_pb2_grpc as rpc
from .codec import decode_server_event, encode_client_event


class AhaGrpcConnection:
    def __init__(self, call: grpc.aio.StreamStreamCall) -> None:
        self.call = call
        self._write_lock = asyncio.Lock()
        self._input_finished = False

    async def send(self, event: dict[str, Any] | pb.ClientEvent) -> None:
        message = (
            event if isinstance(event, pb.ClientEvent) else encode_client_event(event)
        )
        async with self._write_lock:
            if self._input_finished:
                raise RuntimeError("aha-grpc 请求流已经结束")
            await self.call.write(message)

    async def finish_input(self) -> None:
        async with self._write_lock:
            if not self._input_finished:
                await self.call.done_writing()
                self._input_finished = True

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Use one reader; events and sends run concurrently."""
        while True:
            message = await self.call.read()
            if message is grpc.aio.EOF:
                return
            yield decode_server_event(message)

    async def __aenter__(self) -> AhaGrpcConnection:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.call.cancel()


class AhaGrpcClient:
    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._stub = rpc.AhaRealtimeStub(channel)

    def connect(
        self,
        *,
        intent: str | None = None,
        model: str | None = None,
        diagnostics: bool = False,
        timeout: float | None = None,
    ) -> AhaGrpcConnection:
        if (intent is None) == (model is None):
            raise ValueError("必须且只能指定 intent 或 model")
        metadata = [("intent", intent)] if intent is not None else [("model", model)]
        metadata.append(("diagnostics", "true" if diagnostics else "false"))
        return AhaGrpcConnection(self._stub.Connect(metadata=metadata, timeout=timeout))
