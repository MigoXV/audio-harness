from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer

from audio_harness.commands.types import (
    GrpcHostOption,
    GrpcPortOption,
    HttpHostOption,
    HttpPortOption,
    LogLevelOption,
    MaxMessageBytesOption,
    TlsCertFileOption,
    TlsKeyFileOption,
    WebRtcDisconnectedGraceOption,
    WebRtcIceServersJsonOption,
    WebRtcNegotiationTimeoutOption,
)
from audio_harness.configs import AppConfig, IceServerConfig, parse_ice_servers_json
from audio_harness.database import require_fresh_database, upgrade_database
from audio_harness.serving.runner import run_server

logger = logging.getLogger(__name__)


def apply_serve_overrides(
    config: AppConfig,
    *,
    grpc_host: str | None,
    grpc_port: int | None,
    max_message_bytes: int,
    http_host: str | None,
    http_port: int | None,
    tls_cert_file: Path,
    tls_key_file: Path,
    ice_servers: list[IceServerConfig],
    webrtc_negotiation_timeout_seconds: float,
    webrtc_disconnected_grace_seconds: float,
) -> AppConfig:
    grpc_config = config.grpc.model_copy(
        update={
            **({"host": grpc_host} if grpc_host is not None else {}),
            **({"port": grpc_port} if grpc_port is not None else {}),
        }
    )
    http_config = config.http.model_copy(
        update={
            **({"host": http_host} if http_host is not None else {}),
            **({"port": http_port} if http_port is not None else {}),
            "tls_cert_file": tls_cert_file,
            "tls_key_file": tls_key_file,
        }
    )
    return config.model_copy(
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


def serve(
    grpc_host: GrpcHostOption = None,
    grpc_port: GrpcPortOption = None,
    max_message_bytes: MaxMessageBytesOption = 4 * 1024 * 1024,
    http_host: HttpHostOption = None,
    http_port: HttpPortOption = None,
    tls_cert_file: TlsCertFileOption = Path("model-bin/realtime-tls/cert.pem"),
    tls_key_file: TlsKeyFileOption = Path("model-bin/realtime-tls/key.pem"),
    webrtc_ice_servers_json: WebRtcIceServersJsonOption = "[]",
    webrtc_negotiation_timeout_seconds: WebRtcNegotiationTimeoutOption = 10.0,
    webrtc_disconnected_grace_seconds: WebRtcDisconnectedGraceOption = 10.0,
    log_level: LogLevelOption = "INFO",
) -> None:
    """启动多工作流语音转写与 Realtime 语音智能体服务。"""

    logging.getLogger().setLevel(log_level.upper())
    try:
        ice_servers = parse_ice_servers_json(webrtc_ice_servers_json)
    except ValueError as exc:
        raise typer.BadParameter(
            str(exc),
            param_hint="--webrtc-ice-servers-json",
        ) from exc
    config = apply_serve_overrides(
        AppConfig.from_environment(),
        grpc_host=grpc_host,
        grpc_port=grpc_port,
        max_message_bytes=max_message_bytes,
        http_host=http_host,
        http_port=http_port,
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
        ice_servers=ice_servers,
        webrtc_negotiation_timeout_seconds=(
            webrtc_negotiation_timeout_seconds
        ),
        webrtc_disconnected_grace_seconds=(
            webrtc_disconnected_grace_seconds
        ),
    )
    require_fresh_database(config.database.url)
    logger.info("升级 SQLite 数据库: url=%s", config.database.url)
    upgrade_database(config.database.url)
    asyncio.run(run_server(config))
