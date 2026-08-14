from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from audio_harness.configs import AppConfig
from audio_harness.database import require_fresh_database, upgrade_database
from audio_harness.errors import (
    EngineDeadlineError,
    EngineUnavailableError,
)
from audio_harness.serving.host import AudioHarnessHost


class ReadyInferencer:
    def __init__(self) -> None:
        self.closed = False

    async def ready(self) -> None:
        return None

    async def probe(self) -> None:
        return None

    async def list_models(self) -> tuple[str, ...]:
        return ("model-z", "model-a")

    async def close(self) -> None:
        self.closed = True


class ReadyFactory:
    def __init__(self) -> None:
        self.created: list[ReadyInferencer] = []

    def create(self, _snapshot) -> ReadyInferencer:
        inferencer = ReadyInferencer()
        self.created.append(inferencer)
        return inferencer


class FailingModelInferencer(ReadyInferencer):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def list_models(self) -> tuple[str, ...]:
        raise self.error


class FailingModelFactory:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def create(self, _snapshot) -> FailingModelInferencer:
        return FailingModelInferencer(self.error)


@dataclass
class ControlPlane:
    host: AudioHarnessHost
    client: httpx.AsyncClient


@pytest_asyncio.fixture
async def control_plane(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    upgrade_database(database_url)
    config = AppConfig.model_validate(
        {
            "database": {"url": database_url},
            "voices": {
                "storage_dir": tmp_path / "voices",
                "max_upload_bytes": 1024 * 1024,
            },
        }
    )
    host = AudioHarnessHost(config)
    host.inferencer_pool._create_inferencer = ReadyFactory().create
    await host.workflow_settings.initialize()
    capacity = await host.capacity_settings.initialize()
    await host.capacity.apply(capacity)
    await host.audio_workflows.initialize()
    await host.voice_agents.initialize()
    await host.realtime_routes.initialize()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.http_app),
        base_url="http://test",
    ) as client:
        yield ControlPlane(host=host, client=client)
    await host.stop()


def engine_payload(
    slug: str,
    *,
    kind: str,
    api: str,
    protocol: str = "grpc",
) -> dict:
    payload = {
        "slug": slug,
        "name": slug,
        "kind": kind,
        "api": api,
        "protocol": protocol,
        "target": "https://example.test/v1" if protocol == "https" else "test:1",
        "connect_timeout_seconds": 1,
        "enabled": True,
        "status": "healthy",
    }
    if api == "responses":
        payload["api_key"] = "test-key"
    return payload


async def create_audio_engines(client: httpx.AsyncClient) -> tuple[str, str]:
    vad = await client.post(
        "/api/v1/engines",
        json=engine_payload("vad", kind="vad", api="ux_vad"),
    )
    asr = await client.post(
        "/api/v1/engines",
        json=engine_payload("asr", kind="asr", api="ux_speech"),
    )
    assert vad.status_code == asr.status_code == 201
    return vad.json()["id"], asr.json()["id"]


async def bind_and_publish_audio_workflow(
    client: httpx.AsyncClient,
) -> dict:
    vad_id, asr_id = await create_audio_engines(client)
    draft = (await client.get("/api/v1/audio-workflows/default-audio/draft")).json()
    document = draft["document"]
    for node in document["nodes"]:
        if node["kind"] == "physical_vad":
            node["engine_id"] = vad_id
        elif node["kind"] == "asr":
            node["engine_id"] = asr_id
    saved = await client.put(
        "/api/v1/audio-workflows/default-audio/draft",
        json={"expected_revision": draft["revision"], "document": document},
    )
    assert saved.status_code == 200
    published = await client.post(
        "/api/v1/audio-workflows/default-audio/publish",
        json={"expected_revision": saved.json()["revision"]},
    )
    assert published.status_code == 200
    return published.json()


@pytest.mark.asyncio
async def test_fresh_baseline_is_explicitly_unconfigured(
    control_plane: ControlPlane,
) -> None:
    engines = await control_plane.client.get("/api/v1/engines")
    assert engines.status_code == 200
    assert engines.json()["total"] == 0

    workflows = await control_plane.client.get("/api/v1/audio-workflows")
    assert [item["id"] for item in workflows.json()] == ["default-audio"]
    draft = await control_plane.client.get(
        "/api/v1/audio-workflows/default-audio/draft"
    )
    assert all(node["engine_id"] is None for node in draft.json()["document"]["nodes"])

    node_types = await control_plane.client.get("/api/v1/audio-node-types")
    assert {item["kind"] for item in node_types.json()} == {
        "physical_vad",
        "semantic_vad",
        "asr",
    }
    readiness = await control_plane.client.get("/health/ready")
    assert readiness.status_code == 503
    assert readiness.json()["data_plane"] == "not_ready"
    overview = await control_plane.client.get("/api/v1/runtime/overview")
    assert overview.status_code == 200
    assert overview.json()["status"] == "needs_configuration"
    assert {item["code"] for item in overview.json()["issues"]} == {
        "grpc_workflow_unpublished",
        "realtime_route_unconfigured",
    }


@pytest.mark.asyncio
async def test_responses_engine_discovers_models_without_exposing_credentials(
    control_plane: ControlPlane,
) -> None:
    factory = ReadyFactory()
    control_plane.host.inferencer_pool._create_inferencer = factory.create
    created = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            "responses-models",
            kind="llm",
            api="responses",
            protocol="https",
        ),
    )
    engine_id = created.json()["id"]

    response = await control_plane.client.get(
        f"/api/v1/engines/{engine_id}/models"
    )

    assert response.status_code == 200
    assert response.json() == {
        "engine_id": engine_id,
        "engine_revision": 1,
        "items": ["model-z", "model-a"],
    }
    assert "api_key" not in response.text
    assert factory.created[-1].closed is True


@pytest.mark.asyncio
async def test_model_discovery_rejects_incompatible_and_disabled_engines(
    control_plane: ControlPlane,
) -> None:
    incompatible = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload("asr-models", kind="asr", api="ux_speech"),
    )
    mismatch = await control_plane.client.get(
        f"/api/v1/engines/{incompatible.json()['id']}/models"
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "engine_model_capability_mismatch"

    responses = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            "disabled-models",
            kind="llm",
            api="responses",
            protocol="https",
        ),
    )
    engine_id = responses.json()["id"]
    disabled = await control_plane.client.patch(
        f"/api/v1/engines/{engine_id}",
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    unavailable = await control_plane.client.get(
        f"/api/v1/engines/{engine_id}/models"
    )
    assert unavailable.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            EngineUnavailableError("provider unavailable"),
            502,
            "engine_model_discovery_failed",
        ),
        (
            EngineDeadlineError("provider timeout"),
            504,
            "engine_model_discovery_timeout",
        ),
    ],
)
async def test_model_discovery_maps_upstream_failures(
    control_plane: ControlPlane,
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    created = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            "failing-models",
            kind="llm",
            api="responses",
            protocol="https",
        ),
    )
    control_plane.host.inferencer_pool._create_inferencer = FailingModelFactory(
        error
    ).create

    response = await control_plane.client.get(
        f"/api/v1/engines/{created.json()['id']}/models"
    )

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code


@pytest.mark.asyncio
async def test_typed_draft_validates_publishes_and_pins_engine_revisions(
    control_plane: ControlPlane,
) -> None:
    published = await bind_and_publish_audio_workflow(control_plane.client)
    assert published["revision"] == 1
    assert set(published["engine_revisions"]) == {"physical-vad", "asr"}

    validation = await control_plane.client.post(
        "/api/v1/audio-workflows/default-audio/validate"
    )
    assert validation.json()["valid"] is True
    assert validation.json()["topological_order"] == ["physical-vad", "asr"]

    readiness = await control_plane.client.get("/health/ready")
    assert readiness.status_code == 200
    active = await control_plane.client.get(
        "/api/v1/audio-workflows/default-audio/active"
    )
    assert active.json()["engine_revisions"] == published["engine_revisions"]


@pytest.mark.asyncio
async def test_referenced_engine_cannot_be_disabled(
    control_plane: ControlPlane,
) -> None:
    await bind_and_publish_audio_workflow(control_plane.client)
    engines = (await control_plane.client.get("/api/v1/engines")).json()["items"]
    vad = next(item for item in engines if item["slug"] == "vad")

    response = await control_plane.client.patch(
        f"/api/v1/engines/{vad['id']}",
        json={"enabled": False},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "revision_conflict"
    assert "草稿" in response.json()["error"]["message"]
    assert "激活修订" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_draft_updates_use_optimistic_revision(
    control_plane: ControlPlane,
) -> None:
    draft = (
        await control_plane.client.get(
            "/api/v1/audio-workflows/default-audio/draft"
        )
    ).json()
    first = await control_plane.client.put(
        "/api/v1/audio-workflows/default-audio/draft",
        json={
            "expected_revision": draft["revision"],
            "document": draft["document"],
        },
    )
    assert first.status_code == 200
    stale = await control_plane.client.put(
        "/api/v1/audio-workflows/default-audio/draft",
        json={
            "expected_revision": draft["revision"],
            "document": draft["document"],
        },
    )
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_voice_agent_is_a_profile_over_a_published_audio_workflow(
    control_plane: ControlPlane,
) -> None:
    await bind_and_publish_audio_workflow(control_plane.client)
    responses = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            "responses",
            kind="llm",
            api="responses",
            protocol="https",
        ),
    )
    tts = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload("tts", kind="tts", api="qwen_tts_duplex"),
    )
    created = await control_plane.client.post(
        "/api/v1/voice-agents",
        json={
            "slug": "assistant",
            "name": "Assistant",
            "definition": {
                "audio_workflow_id": "default-audio",
                "responses_engine_id": responses.json()["id"],
                "responses": {"model": "test-model"},
                "tts_engine_id": tts.json()["id"],
                "tts": {
                    "default_voice": "voice-a",
                    "allowed_voices": ["voice-a"],
                },
            },
        },
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]
    validation = await control_plane.client.post(
        f"/api/v1/voice-agents/{agent_id}/validate"
    )
    assert validation.status_code == 200
    assert validation.json() == {"valid": True, "diagnostics": []}
    published = await control_plane.client.post(
        f"/api/v1/voice-agents/{agent_id}/publish",
        json={"expected_revision": 1},
    )
    assert published.status_code == 200
    assert published.json()["audio_workflow_revision"] == 1

    route = await control_plane.client.put(
        "/api/v1/realtime-routes/assistant",
        json={
            "model": "assistant",
            "target_type": "voice_agent",
            "target_id": agent_id,
            "visibility": "public",
        },
    )
    assert route.status_code == 200
    assert route.json()["target_type"] == "voice_agent"
    fetched_route = await control_plane.client.get(
        "/api/v1/realtime-routes/assistant"
    )
    assert fetched_route.json() == route.json()

    conflict = await control_plane.client.put(
        "/api/v1/realtime-routes/assistant",
        json={
            "model": "assistant",
            "target_type": "audio_workflow",
            "target_id": "default-audio",
            "visibility": "public",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "realtime_route_conflict"
    assert conflict.json()["error"]["details"]["target_id"] == agent_id

    replaced = await control_plane.client.put(
        "/api/v1/realtime-routes/assistant",
        json={
            "model": "assistant",
            "target_type": "audio_workflow",
            "target_id": "default-audio",
            "visibility": "public",
            "replace_existing": True,
        },
    )
    assert replaced.json()["target_type"] == "audio_workflow"
    restored_route = await control_plane.client.put(
        "/api/v1/realtime-routes/assistant",
        json={
            "model": "assistant",
            "target_type": "voice_agent",
            "target_id": agent_id,
            "visibility": "public",
            "replace_existing": True,
        },
    )
    assert restored_route.json()["target_type"] == "voice_agent"

    overview = await control_plane.client.get("/api/v1/runtime/overview")
    assert overview.json()["status"] == "ready"

    active = await control_plane.client.get(
        f"/api/v1/voice-agents/{agent_id}/active"
    )
    assert active.status_code == 200
    history = await control_plane.client.get(
        f"/api/v1/voice-agents/{agent_id}/revisions"
    )
    assert [item["revision"] for item in history.json()] == [1]
    restored = await control_plane.client.post(
        f"/api/v1/voice-agents/{agent_id}/revisions/1/restore",
        json={"expected_revision": 1},
    )
    assert restored.status_code == 200
    assert restored.json()["revision"] == 2

    still_routed = await control_plane.client.delete(
        f"/api/v1/voice-agents/{agent_id}"
    )
    assert still_routed.status_code == 409
    assert (
        await control_plane.client.delete("/api/v1/realtime-routes/assistant")
    ).status_code == 204
    assert (
        await control_plane.client.delete(f"/api/v1/voice-agents/{agent_id}")
    ).status_code == 204


def test_old_database_revision_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    import sqlite3

    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.execute("INSERT INTO alembic_version VALUES ('0009_control_plane_v2')")
    with pytest.raises(RuntimeError, match="不支持原地升级"):
        require_fresh_database(f"sqlite+aiosqlite:///{path}")
