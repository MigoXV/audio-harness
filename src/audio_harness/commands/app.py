from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import logging
import signal
from pathlib import Path
from typing import Optional

import typer
import uvicorn

from audio_harness.config import AppConfig, DatabaseConfig, parse_ice_servers_json
from audio_harness.database import (
    require_fresh_database,
    upgrade_database,
)
from audio_harness.serving import AudioHarnessHost

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


@app.command("export-openapi")
def export_openapi(
    output: Path = typer.Option(
        Path("src/web/openapi.json"),
        "--output",
        dir_okay=False,
        help="OpenAPI JSON 输出路径。",
    ),
) -> None:
    """导出控制面 OpenAPI，供前端生成唯一可信的接口类型。"""

    config = AppConfig.from_environment().model_copy(
        update={"database": DatabaseConfig(url="sqlite+aiosqlite:///:memory:")}
    )
    server = AudioHarnessHost(config)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(server.http_app.openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    typer.echo(f"已导出 OpenAPI: {output}")


async def _serve(config: AppConfig) -> None:
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
            ws_max_size=(
                config.limits.max_message_bytes * 2
            ),
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
            {http_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
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


@app.command("serve")
def serve(
    grpc_host: Optional[str] = typer.Option(
        None,
        "--grpc-host",
        envvar="GRPC_HOST",
        help="覆盖 gRPC 监听地址。",
    ),
    grpc_port: Optional[int] = typer.Option(
        None,
        "--grpc-port",
        envvar="GRPC_PORT",
        min=0,
        max=65535,
        help="覆盖 gRPC 监听端口；设为 0 时自动选择空闲端口。",
    ),
    max_message_bytes: int = typer.Option(
        4 * 1024 * 1024,
        "--max-message-bytes",
        envvar="MAX_MESSAGE_BYTES",
        min=1,
        help="gRPC、WebSocket 与 WebRTC 信令单消息字节上限。",
    ),
    http_host: Optional[str] = typer.Option(
        None,
        "--http-host",
        envvar="HTTP_HOST",
        help="覆盖 HTTP/WS/WebRTC 信令监听地址。",
    ),
    http_port: Optional[int] = typer.Option(
        None,
        "--http-port",
        envvar="HTTP_PORT",
        min=0,
        max=65535,
        help="覆盖 HTTP/WS/WebRTC 信令监听端口；设为 0 时自动选择空闲端口。",
    ),
    tls_cert_file: Path = typer.Option(
        Path("model-bin/realtime-tls/cert.pem"),
        "--tls-cert-file",
        envvar="HTTP_TLS_CERT_FILE",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="HTTP/WS TLS 证书。",
    ),
    tls_key_file: Path = typer.Option(
        Path("model-bin/realtime-tls/key.pem"),
        "--tls-key-file",
        envvar="HTTP_TLS_KEY_FILE",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="HTTP/WS TLS 私钥。",
    ),
    webrtc_ice_servers_json: str = typer.Option(
        "[]",
        "--webrtc-ice-servers-json",
        envvar="WEBRTC_ICE_SERVERS_JSON",
        help="WebRTC ICE servers JSON 数组。",
    ),
    webrtc_negotiation_timeout_seconds: float = typer.Option(
        10.0,
        "--webrtc-negotiation-timeout-seconds",
        envvar="WEBRTC_NEGOTIATION_TIMEOUT_SECONDS",
        min=0.1,
        help="WebRTC SDP/ICE 协商超时秒数。",
    ),
    webrtc_disconnected_grace_seconds: float = typer.Option(
        10.0,
        "--webrtc-disconnected-grace-seconds",
        envvar="WEBRTC_DISCONNECTED_GRACE_SECONDS",
        min=0,
        help="WebRTC disconnected 状态保留秒数。",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        envvar="LOG_LEVEL",
        help="日志级别。",
    ),
) -> None:
    """启动多工作流语音转写与 Realtime 语音智能体服务。"""

    logging.getLogger().setLevel(log_level.upper())
    config = AppConfig.from_environment()
    try:
        ice_servers = parse_ice_servers_json(webrtc_ice_servers_json)
    except ValueError as exc:
        raise typer.BadParameter(
            str(exc),
            param_hint="--webrtc-ice-servers-json",
        ) from exc
    grpc_config = config.grpc.model_copy(
        update={
            **({"host": grpc_host} if grpc_host is not None else {}),
            **({"port": grpc_port} if grpc_port is not None else {}),
        }
    )
    http_config = config.http.model_copy(
        update={
            **(
                {"host": http_host}
                if http_host is not None
                else {}
            ),
            **(
                {"port": http_port}
                if http_port is not None
                else {}
            ),
            "tls_cert_file": tls_cert_file,
            "tls_key_file": tls_key_file,
        }
    )
    config = config.model_copy(
        update={
            "grpc": grpc_config,
            "http": http_config,
            "limits": config.limits.model_copy(
                update={"max_message_bytes": max_message_bytes}
            ),
            "webrtc": config.webrtc.model_copy(
                update={
                    "ice_servers": ice_servers,
                    "negotiation_timeout_seconds": (
                        webrtc_negotiation_timeout_seconds
                    ),
                    "disconnected_grace_seconds": (
                        webrtc_disconnected_grace_seconds
                    ),
                }
            ),
        }
    )
    require_fresh_database(config.database.url)
    logger.info("升级 SQLite 数据库: url=%s", config.database.url)
    upgrade_database(config.database.url)
    asyncio.run(_serve(config))


if __name__ == "__main__":
    app()
