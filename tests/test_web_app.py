from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from audio_harness.api.service import EngineCatalog
from audio_harness.database import Database, upgrade_database
from audio_harness.engines.router import EngineRouter
from audio_harness.inferencers.grpc import GrpcInferencerFactory
from audio_harness.serving.http import create_http_app


async def build_app(tmp_path: Path, web_dist: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'web-control.db'}"
    upgrade_database(database_url)
    database = Database(database_url)
    router = EngineRouter(
        database.session_factory,
        inferencer_factory=GrpcInferencerFactory(
            min_chunk_bytes=4,
            max_message_bytes=1024 * 1024,
        ),
    )
    catalog = EngineCatalog(database.session_factory, router)
    app = create_http_app(
        database=database,
        catalog=catalog,
        engine_router=router,
        web_dist=web_dist,
    )
    return app, database, router


@pytest.mark.asyncio
async def test_api_remains_available_when_web_is_not_built(
    tmp_path: Path,
) -> None:
    app, database, router = await build_app(tmp_path, tmp_path / "missing")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            root = await client.get("/")
            assert root.status_code == 503
            assert "Web 配置界面尚未构建" in root.text
            orchestration = await client.get("/orchestration")
            assert orchestration.status_code == 503
            assert "Web 配置界面尚未构建" in orchestration.text

            docs = await client.get("/docs")
            assert docs.status_code == 200

            engines = await client.get("/api/v1/engines")
            assert engines.status_code == 200
    finally:
        await router.close()
        await database.close()


@pytest.mark.asyncio
async def test_built_web_is_served_without_shadowing_api(
    tmp_path: Path,
) -> None:
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

    app, database, router = await build_app(tmp_path, web_dist)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            root = await client.get("/")
            assert root.status_code == 200
            assert "Audio Harness Web" in root.text

            for path in ("/orchestration", "/engines", "/settings", "/missing"):
                page = await client.get(path)
                assert page.status_code == 200
                assert "Audio Harness Web" in page.text

            asset = await client.get("/assets/app.js")
            assert asset.status_code == 200
            assert "audioHarness" in asset.text

            engines = await client.get("/api/v1/engines")
            assert engines.status_code == 200
            assert engines.json()["total"] == 11

            missing_api = await client.get("/api/v1/not-found")
            assert missing_api.status_code == 404
            assert "Audio Harness Web" not in missing_api.text

            missing_health = await client.get("/health/not-found")
            assert missing_health.status_code == 404
            assert "Audio Harness Web" not in missing_health.text
    finally:
        await router.close()
        await database.close()
