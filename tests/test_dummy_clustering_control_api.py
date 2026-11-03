from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from audio_harness.configs.app import AppConfig
from audio_harness.database.session import upgrade_database
from audio_harness.serving.host import AudioHarnessHost
from tests.test_engine_control_api import engine_payload


@pytest_asyncio.fixture
async def control_plane(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    upgrade_database(database_url)
    host = AudioHarnessHost(AppConfig.model_validate({
        "database": {"url": database_url},
        "voices": {"storage_dir": tmp_path / "voices"},
    }))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host.http_app), base_url="http://test",
        ) as client:
            yield SimpleNamespace(client=client)
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_dummy_clustering_engine_create_edit_and_probe(control_plane) -> None:
    payload = engine_payload("dummy-clustering", kind="embedding", api="dummy_clustering", protocol="dummy")
    payload["target"] = "dummy://clustering"
    response = await control_plane.client.post("/api/v1/engines", json=payload)
    assert response.status_code == 201
    engine_id = response.json()["id"]
    response = await control_plane.client.patch(f"/api/v1/engines/{engine_id}", json={"name": "占位聚类"})
    assert response.status_code == 200
    assert response.json()["api"] == "dummy_clustering"
    response = await control_plane.client.post(f"/api/v1/engines/{engine_id}/validate")
    assert response.status_code == 200
    assert response.json()["results"][0]["outcome"] == "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"protocol": "grpc"}, {"target": "dummy://other"},
    {"kind": "diarization"}, {"api_key": "secret"}, {"credential_env": "TOKEN"},
])
async def test_dummy_clustering_rejects_invalid_shape(control_plane, changes: dict) -> None:
    payload = engine_payload("dummy-clustering", kind="embedding", api="dummy_clustering", protocol="dummy")
    payload["target"] = "dummy://clustering"
    payload.update(changes)
    response = await control_plane.client.post("/api/v1/engines", json=payload)
    assert response.status_code == 409
