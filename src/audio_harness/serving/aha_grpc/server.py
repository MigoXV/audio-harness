"""gRPC framing and lifecycle adapter for the shared Realtime service."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import grpc

from audio_harness.configs.app import AppConfig
from audio_harness.errors import CapacityExceededError
from audio_harness.protos.aha_grpc.v1 import aha_realtime_pb2 as pb
from audio_harness.protos.aha_grpc.v1 import aha_realtime_pb2_grpc as rpc
from audio_harness.serving.realtime.core import (
    RealtimeConnection,
    RealtimeConnectionRejected,
    RealtimeOptions,
    RealtimeService,
)
from .codec import decode_client_event, encode_server_event

logger = logging.getLogger(__name__)

_STATUS = {
    "invalid_argument": grpc.StatusCode.INVALID_ARGUMENT,
    "not_found": grpc.StatusCode.NOT_FOUND,
    "failed_precondition": grpc.StatusCode.FAILED_PRECONDITION,
    "internal": grpc.StatusCode.INTERNAL,
}


def connection_options(metadata: Any) -> RealtimeOptions:
    values: dict[str, str] = {}
    for key, value in metadata:
        if key not in {"intent", "model", "x_aha", "diagnostics"}:
            continue
        if key in values or not isinstance(value, str):
            raise RealtimeConnectionRejected(
                "invalid_argument", f"Invalid metadata: {key}"
            )
        values[key] = value
    diagnostics = values.get("diagnostics", "false")
    if diagnostics not in {"true", "false"}:
        raise RealtimeConnectionRejected(
            "invalid_argument", "diagnostics must be true or false"
        )
    return RealtimeOptions(
        intent=values.get("intent"),
        model=values.get("model"),
        diagnostics_enabled=diagnostics == "true",
        aha_version=values.get("x_aha", "v1"),
    )


class AhaRealtimeServicer(rpc.AhaRealtimeServicer):
    def __init__(self, realtime: RealtimeService) -> None:
        self.realtime = realtime
        self._calls: set[asyncio.Task[Any]] = set()
        self._cleanups: set[asyncio.Task[Any]] = set()

    async def Connect(
        self,
        request_iterator: AsyncIterator[pb.ClientEvent],
        context: grpc.aio.ServicerContext,
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._calls.add(task)
        try:
            await self._connect(request_iterator, context)
        finally:
            self._calls.discard(task)

    async def wait_closed(self) -> None:
        # gRPC may finish cancellation before a handler's async cleanup ends.
        # Keep cleanup alive and join it before Host releases shared engines.
        while self._calls or self._cleanups:
            await asyncio.gather(*self._calls, *self._cleanups, return_exceptions=True)

    async def _connect(
        self,
        request_iterator: AsyncIterator[pb.ClientEvent],
        context: grpc.aio.ServicerContext,
    ) -> None:
        connection: RealtimeConnection | None = None
        tasks: list[asyncio.Task[Any]] = []
        try:
            connection = await self.realtime.open(
                connection_options(context.invocation_metadata())
            )

            async def receive() -> None:
                assert connection is not None
                async for request in request_iterator:
                    try:
                        event = decode_client_event(request)
                    except ValueError as exc:
                        selected = request.WhichOneof("event")
                        event_id = (
                            getattr(getattr(request, selected), "event_id", None)
                            if selected
                            else None
                        )
                        await connection.send_error(
                            message=str(exc),
                            code="invalid_event",
                            client_event_id=event_id,
                        )
                        continue
                    await connection.handle_event(event)
                await connection.finish_input()
                await connection.drain_events()

            async def send() -> None:
                assert connection is not None
                async for event in connection.events():
                    await context.write(encode_server_event(event))

            tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except RealtimeConnectionRejected as exc:
            await context.abort(_STATUS[exc.code], str(exc))
        except CapacityExceededError:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED, "Session capacity exceeded"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("aha-grpc 会话失败")
            await context.abort(grpc.StatusCode.INTERNAL, "Realtime session failed")
        finally:

            async def cleanup() -> None:
                if connection is not None:
                    connection.close_output()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if connection is not None:
                    await connection.close()

            cleanup_task = asyncio.create_task(cleanup(), name="aha-grpc-cleanup")
            self._cleanups.add(cleanup_task)
            cleanup_task.add_done_callback(self._cleanups.discard)
            await asyncio.shield(cleanup_task)


class AhaGrpcServer:
    def __init__(self, config: AppConfig, realtime: RealtimeService) -> None:
        self.config = config
        self.realtime = realtime
        self._server: grpc.aio.Server | None = None
        self._servicer = AhaRealtimeServicer(realtime)
        self.port: int | None = None

    async def start(self) -> None:
        cert = self.config.http.tls_cert_file
        key = self.config.http.tls_key_file
        if cert is None or key is None:
            raise ValueError("aha-grpc 必须配置现有 HTTP TLS 证书和私钥")
        credentials = grpc.ssl_server_credentials(
            ((key.read_bytes(), cert.read_bytes()),)
        )
        limit = self.config.limits.max_message_bytes
        server = grpc.aio.server(
            options=[
                ("grpc.max_receive_message_length", limit),
                ("grpc.max_send_message_length", limit),
                ("grpc.so_reuseport", 0),
            ]
        )
        rpc.add_AhaRealtimeServicer_to_server(self._servicer, server)
        address = f"{self.config.aha_grpc.host}:{self.config.aha_grpc.port}"
        try:
            self.port = server.add_secure_port(address, credentials)
            if not self.port:
                raise RuntimeError(f"aha-grpc 绑定失败: {address}")
            await server.start()
        except BaseException:
            await server.stop(0)
            raise
        self._server = server
        logger.info(
            "aha-grpc TLS 已启动: address=%s:%s", self.config.aha_grpc.host, self.port
        )

    async def wait_for_termination(self) -> None:
        if self._server is None:
            raise RuntimeError("aha-grpc 尚未启动")
        await self._server.wait_for_termination()

    async def stop(self) -> None:
        if self._server is not None:
            server, self._server = self._server, None
            await server.stop(5.0)
            await self._servicer.wait_closed()
