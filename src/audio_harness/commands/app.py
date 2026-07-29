from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Optional

import typer

from audio_harness.config import AppConfig
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


async def _serve(config: AppConfig) -> None:
    server = AudioHarnessServer(config)
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
    try:
        await stop_event.wait()
    finally:
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
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        envvar="AUDIO_HARNESS_LOG_LEVEL",
        help="日志级别。",
    ),
) -> None:
    """启动异步 ASR + VAD gRPC 服务。"""

    logging.getLogger().setLevel(log_level.upper())
    config = AppConfig.load(config_path)
    serving = config.serving.model_copy(
        update={
            **({"host": host} if host is not None else {}),
            **({"port": port} if port is not None else {}),
        }
    )
    asyncio.run(_serve(config.model_copy(update={"serving": serving})))


if __name__ == "__main__":
    app()
