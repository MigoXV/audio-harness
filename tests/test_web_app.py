from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from audio_harness.config import AppConfig
from audio_harness.database import upgrade_database
from audio_harness.serving.host import AudioHarnessHost
from audio_harness.serving.http import create_http_app


async def build_app(tmp_path: Path, web_dist: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'web-control.db'}"
    upgrade_database(database_url)
    host = AudioHarnessHost(
        AppConfig.model_validate(
            {
                "database": {"url": database_url},
                "voices": {"storage_dir": tmp_path / "voices"},
            }
        )
    )
    await host.workflow_settings.initialize()
    capacity = await host.capacity_settings.initialize()
    await host.capacity.apply(capacity)
    await host.audio_workflows.initialize()
    await host.voice_agents.initialize()
    await host.realtime_routes.initialize()
    app = create_http_app(
        catalog=host.catalog,
        workflows=host.audio_workflows,
        voice_agents=host.voice_agents,
        routes=host.realtime_routes,
        serving_status=host.serving_status,
        workflow_settings=host.workflow_settings,
        capacity_settings=host.capacity_settings,
        capacity=host.capacity,
        realtime_service=host.realtime_service,
        webrtc_service=host.webrtc_service,
        max_message_bytes=host.config.limits.max_message_bytes,
        voice_store=host.voice_store,
        web_dist=web_dist,
    )
    return app, host


@pytest.mark.asyncio
async def test_api_remains_available_when_web_is_not_built(tmp_path: Path) -> None:
    app, host = await build_app(tmp_path, tmp_path / "missing")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            assert (await client.get("/")).status_code == 404
            assert (await client.get("/docs")).status_code == 200
            engines = await client.get("/api/v1/engines")
            assert engines.status_code == 200
            assert engines.json()["total"] == 0
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_built_web_is_served_without_shadowing_api(tmp_path: Path) -> None:
    web_dist = tmp_path / "dist"
    assets = web_dist / "assets"
    assets.mkdir(parents=True)
    (web_dist / "index.html").write_text(
        "<!doctype html><title>Audio Harness Web</title>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text(
        "globalThis.audioHarness = true;",
        encoding="utf-8",
    )
    (web_dist / "audio-harness-logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><title>Audio Harness 标志</title></svg>',
        encoding="utf-8",
    )
    app, host = await build_app(tmp_path, web_dist)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for path in ("/", "/audio-workflows", "/engines", "/missing"):
                page = await client.get(path)
                assert page.status_code == 200
                assert "Audio Harness Web" in page.text
            assert "audioHarness" in (await client.get("/assets/app.js")).text
            logo = await client.get("/audio-harness-logo.svg")
            assert logo.status_code == 200
            assert logo.headers["content-type"].startswith("image/svg+xml")
            assert "Audio Harness 标志" in logo.text
            assert (await client.get("/api/v1/engines")).status_code == 200
            missing_api = await client.get("/api/v1/not-found")
            assert missing_api.status_code == 404
            assert "Audio Harness Web" not in missing_api.text
            missing_health = await client.get("/health/not-found")
            assert missing_health.status_code == 404
            assert "Audio Harness Web" not in missing_health.text
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_webrtc_rejects_unsupported_x_aha_version(tmp_path: Path) -> None:
    app, host = await build_app(tmp_path, tmp_path / "missing")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post("/v1/realtime/calls?x_aha=v2")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unsupported_x_aha_version"
    finally:
        await host.stop()
