from __future__ import annotations

import asyncio
from contextlib import contextmanager
import logging
import signal

import uvicorn

from audio_harness.configs.app import AppConfig
from audio_harness.serving.host import AudioHarnessHost

logger = logging.getLogger(__name__)


class _ManagedUvicornServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


async def run_server(config: AppConfig) -> None:
    server = AudioHarnessHost(config)
    http_server = _ManagedUvicornServer(
        uvicorn.Config(
            server.http_app,
            host=config.http.host,
            port=config.http.port,
            log_level=logging.getLevelName(logging.getLogger().level).lower(),
            log_config=None,
            lifespan="on",
            timeout_graceful_shutdown=5.0,
            ssl_certfile=(
                str(config.http.tls_cert_file)
                if config.http.tls_cert_file is not None
                else None
            ),
            ssl_keyfile=(
                str(config.http.tls_key_file)
                if config.http.tls_key_file is not None
                else None
            ),
            ws_max_size=config.limits.max_message_bytes * 2,
        )
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        if not stop_event.is_set():
            logger.info("收到关闭信号")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: request_shutdown())

    await server.start()
    http_task = asyncio.create_task(
        http_server.serve(),
        name="audio-harness-http",
    )
    grpc_task = asyncio.create_task(
        server.grpc_server.wait_for_termination(),
        name="audio-harness-grpc",
    )
    stop_task = asyncio.create_task(
        stop_event.wait(),
        name="audio-harness-shutdown-waiter",
    )
    logger.info(
        "Audio Harness HTTP/WS 正在启动: address=%s:%s",
        config.http.host,
        config.http.port,
    )
    try:
        done, _pending = await asyncio.wait(
            {http_task, grpc_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if grpc_task in done:
            await grpc_task
            if not stop_event.is_set():
                raise RuntimeError("aha-grpc 服务意外停止")
        if http_task in done:
            await http_task
            if not stop_event.is_set():
                raise RuntimeError("HTTP/WS 服务意外停止")
    finally:
        http_server.should_exit = True
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        await asyncio.gather(http_task, return_exceptions=True)
        await server.stop()
        await asyncio.gather(grpc_task, return_exceptions=True)
