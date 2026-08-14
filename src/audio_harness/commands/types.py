from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer


GrpcHostOption = Annotated[
    str | None,
    typer.Option(
        "--grpc-host",
        envvar="GRPC_HOST",
        help="覆盖 gRPC 监听地址。",
    ),
]
GrpcPortOption = Annotated[
    int | None,
    typer.Option(
        "--grpc-port",
        envvar="GRPC_PORT",
        min=0,
        max=65535,
        help="覆盖 gRPC 监听端口；设为 0 时自动选择空闲端口。",
    ),
]
MaxMessageBytesOption = Annotated[
    int,
    typer.Option(
        "--max-message-bytes",
        envvar="MAX_MESSAGE_BYTES",
        min=1,
        help="gRPC、WebSocket 与 WebRTC 信令单消息字节上限。",
    ),
]
HttpHostOption = Annotated[
    str | None,
    typer.Option(
        "--http-host",
        envvar="HTTP_HOST",
        help="覆盖 HTTP/WS/WebRTC 信令监听地址。",
    ),
]
HttpPortOption = Annotated[
    int | None,
    typer.Option(
        "--http-port",
        envvar="HTTP_PORT",
        min=0,
        max=65535,
        help=(
            "覆盖 HTTP/WS/WebRTC 信令监听端口；"
            "设为 0 时自动选择空闲端口。"
        ),
    ),
]
TlsCertFileOption = Annotated[
    Path,
    typer.Option(
        "--tls-cert-file",
        envvar="HTTP_TLS_CERT_FILE",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="HTTP/WS TLS 证书。",
    ),
]
TlsKeyFileOption = Annotated[
    Path,
    typer.Option(
        "--tls-key-file",
        envvar="HTTP_TLS_KEY_FILE",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="HTTP/WS TLS 私钥。",
    ),
]
WebRtcIceServersJsonOption = Annotated[
    str,
    typer.Option(
        "--webrtc-ice-servers-json",
        envvar="WEBRTC_ICE_SERVERS_JSON",
        help="WebRTC ICE servers JSON 数组。",
    ),
]
WebRtcNegotiationTimeoutOption = Annotated[
    float,
    typer.Option(
        "--webrtc-negotiation-timeout-seconds",
        envvar="WEBRTC_NEGOTIATION_TIMEOUT_SECONDS",
        min=0.1,
        help="WebRTC SDP/ICE 协商超时秒数。",
    ),
]
WebRtcDisconnectedGraceOption = Annotated[
    float,
    typer.Option(
        "--webrtc-disconnected-grace-seconds",
        envvar="WEBRTC_DISCONNECTED_GRACE_SECONDS",
        min=0,
        help="WebRTC disconnected 状态保留秒数。",
    ),
]
LogLevelOption = Annotated[
    str,
    typer.Option(
        "--log-level",
        envvar="LOG_LEVEL",
        help="日志级别。",
    ),
]
