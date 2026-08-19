from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from audio_harness.commands.app import app
from audio_harness.commands.serve import apply_serve_overrides
from audio_harness.configs import AppConfig, IceServerConfig
from audio_harness.serving import runner as runner_module


def test_cli_only_exposes_service_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "serve" in result.output
    assert "export-openapi" not in result.output


def test_serve_only_exposes_http_listener_options() -> None:
    result = CliRunner().invoke(app, ["serve", "--help"])

    assert result.exit_code == 0
    assert "--http-port" in result.output
    assert "--grpc-port" not in result.output
    assert "--grpc-host" not in result.output


def test_apply_serve_overrides_preserves_unspecified_addresses(
    tmp_path: Path,
) -> None:
    config = AppConfig.model_validate(
        {
            "http": {"host": "http.default", "port": 8000},
        }
    )
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    ice_servers = [IceServerConfig(urls="stun:ice.example:3478")]

    updated = apply_serve_overrides(
        config,
        max_message_bytes=1024,
        http_host="127.0.0.1",
        http_port=None,
        tls_cert_file=cert_file,
        tls_key_file=key_file,
        ice_servers=ice_servers,
        webrtc_negotiation_timeout_seconds=3.0,
        webrtc_disconnected_grace_seconds=4.0,
    )

    assert updated.http.host == "127.0.0.1"
    assert updated.http.port == 8000
    assert updated.http.tls_cert_file == cert_file
    assert updated.http.tls_key_file == key_file
    assert updated.limits.max_message_bytes == 1024
    assert updated.webrtc.ice_servers == ice_servers
    assert updated.webrtc.negotiation_timeout_seconds == 3.0
    assert updated.webrtc.disconnected_grace_seconds == 4.0


@pytest.mark.asyncio
async def test_runner_stops_host_when_http_server_exits_unexpectedly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeHost:
        def __init__(self, _config: AppConfig) -> None:
            self.http_app = object()

        async def start(self) -> None:
            events.append("host.start")

        async def stop(self) -> None:
            events.append("host.stop")

    class FakeHttpServer:
        instance: "FakeHttpServer | None" = None

        def __init__(self, _config) -> None:
            self.should_exit = False
            type(self).instance = self

        async def serve(self) -> None:
            events.append("http.serve")

    monkeypatch.setattr(runner_module, "AudioHarnessHost", FakeHost)
    monkeypatch.setattr(
        runner_module,
        "_ManagedUvicornServer",
        FakeHttpServer,
    )

    with pytest.raises(RuntimeError, match="HTTP/WS 服务意外停止"):
        await runner_module.run_server(AppConfig())

    assert events == ["host.start", "http.serve", "host.stop"]
    assert FakeHttpServer.instance is not None
    assert FakeHttpServer.instance.should_exit is True
