from __future__ import annotations

import asyncio
from contextlib import contextmanager
import logging
import signal
from pathlib import Path
from typing import Optional

import typer
import uvicorn

from audio_harness.config import AppConfig
from audio_harness.database import upgrade_database
from audio_harness.server import AudioHarnessServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help="Audio Harness 服务管理命令。")


@app.callback()
def root() -> None:
    """Audio Harness 服务管理命令。"""


class _ManagedUvicornServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


async def _serve(config: AppConfig) -> None:
    server = AudioHarnessServer(config)
    http_server = _ManagedUvicornServer(
        uvicorn.Config(
            server.management_app,
            host=config.management.host,
            port=config.management.port,
            log_level=logging.getLevelName(logging.getLogger().level).lower(),
            log_config=None,
            lifespan="on",
            timeout_graceful_shutdown=config.management.shutdown_grace_seconds,
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
        name="audio-harness-management-http",
    )
    stop_task = asyncio.create_task(
        stop_event.wait(),
        name="audio-harness-shutdown-waiter",
    )
    logger.info(
        "Audio Harness REST 管理面正在启动: address=%s:%s",
        config.management.host,
        config.management.port,
    )
    try:
        done, _pending = await asyncio.wait(
            {http_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if http_task in done:
            await http_task
            if not stop_event.is_set():
                raise RuntimeError("REST 管理面意外停止")
    finally:
        http_server.should_exit = True
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        await asyncio.gather(http_task, return_exceptions=True)
        await server.stop()


@app.command("serve")
def serve(
    config_path: Path = typer.Option(
        ...,
        "--config",
        "-c",
        envvar="AUDIO_HARNESS_CONFIG",
        exists=True,
        readable=True,
        resolve_path=True,
        help="静态 YAML 配置文件。",
    ),
    host: Optional[str] = typer.Option(
        None,
        "--host",
        envvar="AUDIO_HARNESS_HOST",
        help="覆盖配置中的监听地址。",
    ),
    port: Optional[int] = typer.Option(
        None,
        "--port",
        envvar="AUDIO_HARNESS_PORT",
        min=1,
        max=65535,
        help="覆盖配置中的监听端口。",
    ),
    management_host: Optional[str] = typer.Option(
        None,
        "--management-host",
        envvar="AUDIO_HARNESS_MANAGEMENT_HOST",
        help="覆盖 REST 管理面的监听地址。",
    ),
    management_port: Optional[int] = typer.Option(
        None,
        "--management-port",
        envvar="AUDIO_HARNESS_MANAGEMENT_PORT",
        min=1,
        max=65535,
        help="覆盖 REST 管理面的监听端口。",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        envvar="AUDIO_HARNESS_LOG_LEVEL",
        help="日志级别。",
    ),
) -> None:
    """启动异步 ASR + VAD gRPC 数据面与 REST 管理面。"""

    logging.getLogger().setLevel(log_level.upper())
    config = AppConfig.load(config_path)
    serving = config.serving.model_copy(
        update={
            **({"host": host} if host is not None else {}),
            **({"port": port} if port is not None else {}),
        }
    )
    management = config.management.model_copy(
        update={
            **(
                {"host": management_host}
                if management_host is not None
                else {}
            ),
            **(
                {"port": management_port}
                if management_port is not None
                else {}
            ),
        }
    )
    config = config.model_copy(
        update={
            "serving": serving,
            "management": management,
        }
    )
    logger.info("升级 SQLite 数据库: url=%s", config.database.url)
    upgrade_database(config.database.url)
    asyncio.run(_serve(config))


if __name__ == "__main__":
    app()
