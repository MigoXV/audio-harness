from __future__ import annotations

from dataclasses import dataclass, replace
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
from audio_harness.inferencers.tts.base import TtsInferencer
from audio_harness.serving.host import AudioHarnessHost
from tests.test_agent_backend_control_api import (
    FakeAgentBackendGateway,
    backend_payload,
)


class ReadyInferencer:
    def __init__(self) -> None:
        self.closed = False

    async def ready(self) -> None:
        return None

    async def probe(self) -> None:
        return None

    async def list_models(self) -> tuple[str, ...]:
        return ("model-z", "model-a")

    async def list_preset_voices(self) -> TtsInferencer.PresetVoiceCatalog:
        return TtsInferencer.PresetVoiceCatalog(
            voices=(
                TtsInferencer.PresetVoice(
                    voice_id="rita-id",
                    name="Rita",
                    language="Chinese",
                    supports_synthesize=True,
                    supports_duplex=True,
                    available_on_instance=True,
                ),
            ),
            serving_mode="duplex",
        )

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
        "target": (
            "https://example.test/v1"
            if protocol == "https"
            else "dummy://diarize"
            if protocol == "dummy"
            else "test:1"
        ),
        "connect_timeout_seconds": 1,
        "enabled": True,
        "status": "healthy",
    }
    if api == "responses":
        payload["api_key"] = "test-key"
    return payload


@pytest.mark.asyncio
async def test_qwen_tts_preset_voice_catalog_is_exposed(
    control_plane: ControlPlane,
) -> None:
    created = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload("qwen-tts", kind="tts", api="qwen_tts_duplex"),
    )
    assert created.status_code == 201

    response = await control_plane.client.get(
        f"/api/v1/engines/{created.json()['id']}/voices"
    )

    assert response.status_code == 200
    assert response.json() == {
        "engine_id": created.json()["id"],
        "engine_revision": 1,
        "serving_mode": "duplex",
        "items": [
            {
                "voice_id": "rita-id",
                "name": "Rita",
                "language": "Chinese",
                "supports_synthesize": True,
                "supports_duplex": True,
                "available_on_instance": True,
            }
        ],
    }


@pytest.mark.asyncio
async def test_received_audio_cache_settings_are_persisted(
    control_plane: ControlPlane,
) -> None:
    current = await control_plane.client.get("/api/v1/settings/workflow")
    assert current.status_code == 200
    payload = current.json()
    assert payload["received_audio_cache_enabled"] is False
    assert payload["received_audio_cache_block_seconds"] == 5
    assert payload["received_audio_cache_file_seconds"] == 300

    payload.update(
        received_audio_cache_enabled=True,
        received_audio_cache_block_seconds=10,
        received_audio_cache_file_seconds=600,
    )
    payload.pop("updated_at")
    updated = await control_plane.client.put(
        "/api/v1/settings/workflow",
        json=payload,
    )

    assert updated.status_code == 200
    assert updated.json()["received_audio_cache_enabled"] is True
    assert updated.json()["received_audio_cache_block_seconds"] == 10
    assert updated.json()["received_audio_cache_file_seconds"] == 600


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
        "automatic_gain_control",
        "streaming_denoise",
        "physical_vad",
        "semantic_vad",
        "asr",
        "audio_embedding",
            "speaker_diarization",
            "speaker_clustering",
            "vector_search",
            "speaker_filter",
        }
    readiness = await control_plane.client.get("/health/ready")
    assert readiness.status_code == 200
    assert readiness.json() == {
        "status": "ready",
        "database": "ready",
        "problems": [],
    }
    overview = await control_plane.client.get("/api/v1/runtime/overview")
    assert overview.status_code == 200
    assert overview.json()["status"] == "needs_configuration"
    assert "grpc" not in overview.json()
    assert {item["code"] for item in overview.json()["issues"]} == {
        "realtime_route_unconfigured",
    }
    workflow_settings = await control_plane.client.get("/api/v1/settings/workflow")
    assert "grpc_audio_workflow_id" not in workflow_settings.json()
    capacity = await control_plane.client.get("/api/v1/settings/capacity")
    runtime = await control_plane.client.get("/api/v1/runtime/status")
    assert "grpc" not in capacity.json()
    assert "grpc" not in runtime.json()
    removed_binding = await control_plane.client.put(
        "/api/v1/settings/grpc-workflow",
        json={"workflow_id": "default-audio", "expected_revision": 1},
    )
    assert removed_binding.status_code == 405


@pytest.mark.asyncio
async def test_diarization_engine_uses_dedicated_kind_and_api(
    control_plane: ControlPlane,
) -> None:
    response = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            "moss-trancribe-diarize",
            kind="diarization",
            api="ux_speaker_diarization",
        ),
    )

    assert response.status_code == 201
    assert response.json()["kind"] == "diarization"
    assert response.json()["api"] == "ux_speaker_diarization"


@pytest.mark.asyncio
async def test_dummy_diarization_engine_uses_local_protocol(
    control_plane: ControlPlane,
) -> None:
    response = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            "dummy-diarize",
            kind="diarization",
            api="dummy_diarize",
            protocol="dummy",
        ),
    )

    assert response.status_code == 201
    assert response.json()["target"] == "dummy://diarize"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api", "protocol", "target"),
    [
        ("dummy_diarize", "grpc", "test:1"),
        ("ux_speaker_diarization", "dummy", "dummy://diarize"),
        ("dummy_diarize", "dummy", "dummy://other"),
    ],
)
async def test_diarization_engine_rejects_invalid_dummy_shape(
    control_plane: ControlPlane,
    api: str,
    protocol: str,
    target: str,
) -> None:
    payload = engine_payload(
        f"invalid-{api.replace('_', '-')}-{protocol}",
        kind="diarization",
        api=api,
        protocol=protocol,
    )
    payload["target"] = target

    response = await control_plane.client.post("/api/v1/engines", json=payload)

    assert response.status_code == 409


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
    assert published.json()["agent_backend_revision"] is None
    assert published.json()["audio_workflow_revision"] == 1
    stored_revision = (
        await control_plane.host.voice_agents.repository.get_revision(
            agent_id,
            1,
        )
    )
    legacy_document = {
        key: value
        for key, value in stored_revision.compiled_profile.items()
        if key != "agent_backend"
    }
    legacy_document["schema_version"] = 1
    restored_legacy = await control_plane.host.voice_agents._restore(
        replace(
            stored_revision,
            compiled_profile=legacy_document,
        )
    )
    assert restored_legacy.definition.schema_version == 1
    assert restored_legacy.agent_backend is None
    assert restored_legacy.agent_backend_context is None

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


@pytest.mark.asyncio
async def test_voice_agent_allows_an_incomplete_draft(
    control_plane: ControlPlane,
) -> None:
    created = await control_plane.client.post(
        "/api/v1/voice-agents",
        json={
            "slug": "empty-agent",
            "name": "Empty Agent",
            "definition": {
                "schema_version": 2,
                "audio_workflow_id": "",
            },
        },
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]

    draft = await control_plane.client.get(
        f"/api/v1/voice-agents/{agent_id}/draft"
    )
    assert draft.status_code == 200
    assert draft.json()["definition"]["audio_workflow_id"] == ""

    validation = await control_plane.client.post(
        f"/api/v1/voice-agents/{agent_id}/validate"
    )
    assert validation.status_code == 200
    assert validation.json()["valid"] is False
    assert {item["code"] for item in validation.json()["diagnostics"]} == {
        "audio_workflow_missing",
        "responses_engine_missing",
        "tts_engine_missing",
    }

    published = await control_plane.client.post(
        f"/api/v1/voice-agents/{agent_id}/publish",
        json={"expected_revision": 1},
    )
    assert published.status_code == 412


@pytest.mark.asyncio
async def test_explicit_speaker_context_requires_a_speaker_filter_output(
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
            "slug": "speaker-context-agent",
            "name": "Speaker Context Agent",
            "definition": {
                "schema_version": 2,
                "audio_workflow_id": "default-audio",
                "responses_engine_id": responses.json()["id"],
                "responses": {"model": "test-model"},
                "tts_engine_id": tts.json()["id"],
                "tts": {
                    "default_voice": "voice-a",
                    "allowed_voices": ["voice-a"],
                },
                "speaker_context": {
                    "enabled": True,
                    "speaker_prompt": "speaker-001 使用简洁中文回复。",
                },
            },
        },
    )
    assert created.status_code == 201
    validation = await control_plane.client.post(
        f"/api/v1/voice-agents/{created.json()['id']}/validate"
    )
    assert validation.status_code == 200
    assert validation.json()["valid"] is False
    assert {item["code"] for item in validation.json()["diagnostics"]} == {
        "speaker_context_source_missing"
    }
    assert "speaker_context_preview" not in validation.json()
    published = await control_plane.client.post(
        f"/api/v1/voice-agents/{created.json()['id']}/publish",
        json={"expected_revision": 1},
    )
    assert published.status_code == 412


@pytest.mark.asyncio
async def test_voice_agent_v2_freezes_agent_backend_snapshot_and_preset(
    control_plane: ControlPlane,
) -> None:
    gateway = FakeAgentBackendGateway()
    control_plane.host.agent_backends._gateway = gateway
    backend = await control_plane.client.post(
        "/api/v1/agent-backends",
        json=backend_payload("voice-dsh"),
    )
    assert backend.status_code == 201
    backend_id = backend.json()["id"]
    await bind_and_publish_audio_workflow(control_plane.client)
    responses = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            "responses-v2",
            kind="llm",
            api="responses",
            protocol="https",
        ),
    )
    tts = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload("tts-v2", kind="tts", api="qwen_tts_duplex"),
    )
    definition = {
        "schema_version": 2,
        "audio_workflow_id": "default-audio",
        "responses_engine_id": responses.json()["id"],
        "responses": {"model": "test-model"},
        "tts_engine_id": tts.json()["id"],
        "tts": {
            "default_voice": "voice-a",
            "allowed_voices": ["voice-a"],
        },
        "agent_backend": {
            "backend_id": backend_id,
            "cwd": "/srv/project",
            "agent_preset": "standard",
            "coordinator_instructions": "文件操作交给 DSH",
            "task_timeout_seconds": 900,
            "interaction_timeout_seconds": 120,
        },
    }
    created = await control_plane.client.post(
        "/api/v1/voice-agents",
        json={
            "slug": "assistant-v2",
            "name": "Assistant V2",
            "definition": definition,
        },
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]
    validation = await control_plane.client.post(
        f"/api/v1/voice-agents/{agent_id}/validate"
    )
    assert validation.json()["valid"] is True
    preview = validation.json()["agent_backend_context_preview"]
    assert '"name":"本地 DSH"' in preview
    assert "http://127.0.0.1:3080" not in preview
    assert "/srv/project" not in preview
    published = await control_plane.client.post(
        f"/api/v1/voice-agents/{agent_id}/publish",
        json={"expected_revision": 1},
    )
    assert published.status_code == 200
    assert published.json()["agent_backend_revision"] == 1
    active = control_plane.host.voice_agents.current(agent_id)
    assert active.agent_backend is not None
    assert active.agent_backend.id == backend_id
    assert active.agent_backend.revision == 1
    stored = await control_plane.host.voice_agents.repository.get_revision(
        agent_id,
        1,
    )
    assert stored.compiled_profile["schema_version"] == 2
    assert stored.compiled_profile["agent_backend"]["revision"] == 1
    assert stored.compiled_profile["agent_backend"]["headers_env"] == {
        "Authorization": "DSH_AUTHORIZATION"
    }
    assert stored.compiled_profile["agent_backend_context"] == preview
    assert active.agent_backend_context == preview

    changed = await control_plane.client.patch(
        f"/api/v1/agent-backends/{backend_id}",
        json={"target": "http://127.0.0.1:3081"},
    )
    assert changed.status_code == 200
    assert changed.json()["revision"] == 2
    assert active.agent_backend.revision == 1
    assert active.agent_backend.target == "http://127.0.0.1:3080"

    missing_preset = dict(definition)
    missing_preset["agent_backend"] = {
        **definition["agent_backend"],
        "agent_preset": "missing",
    }
    updated = await control_plane.client.put(
        f"/api/v1/voice-agents/{agent_id}/draft",
        json={"expected_revision": 1, "definition": missing_preset},
    )
    assert updated.status_code == 200
    invalid = await control_plane.client.post(
        f"/api/v1/voice-agents/{agent_id}/validate"
    )
    assert invalid.json()["valid"] is False
    assert invalid.json()["diagnostics"] == [
        {
            "code": "agent_backend_preset_missing",
            "message": "Agent Backend 不存在 preset: missing",
            "field": "agent_backend.agent_preset",
        }
    ]

    restored_draft = await control_plane.client.put(
        f"/api/v1/voice-agents/{agent_id}/draft",
        json={"expected_revision": 2, "definition": definition},
    )
    assert restored_draft.status_code == 200
    gateway.failure = RuntimeError("DSH offline")
    unavailable = await control_plane.client.post(
        f"/api/v1/voice-agents/{agent_id}/validate"
    )
    assert unavailable.json()["diagnostics"][0]["code"] == (
        "agent_backend_unavailable"
    )
    gateway.failure = None

    disabled_payload = backend_payload("disabled-dsh")
    disabled_payload["enabled"] = False
    disabled = await control_plane.client.post(
        "/api/v1/agent-backends",
        json=disabled_payload,
    )
    disabled_definition = dict(definition)
    disabled_definition["agent_backend"] = {
        **definition["agent_backend"],
        "backend_id": disabled.json()["id"],
    }
    disabled_draft = await control_plane.client.put(
        f"/api/v1/voice-agents/{agent_id}/draft",
        json={"expected_revision": 3, "definition": disabled_definition},
    )
    assert disabled_draft.status_code == 200
    disabled_validation = await control_plane.client.post(
        f"/api/v1/voice-agents/{agent_id}/validate"
    )
    assert disabled_validation.json()["diagnostics"][0]["code"] == (
        "agent_backend_disabled"
    )


def test_old_database_revision_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    import sqlite3

    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.execute("INSERT INTO alembic_version VALUES ('0009_control_plane_v2')")
    with pytest.raises(RuntimeError, match="不支持原地升级"):
        require_fresh_database(f"sqlite+aiosqlite:///{path}")


def test_vector_space_registry_revision_can_be_cleaned_up(tmp_path: Path) -> None:
    path = tmp_path / "vector-registry.db"
    import sqlite3

    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.execute("INSERT INTO alembic_version VALUES ('0002_vector_search')")
        connection.execute("CREATE TABLE vector_spaces (id VARCHAR(36) PRIMARY KEY, slug VARCHAR(100))")
        connection.execute("CREATE UNIQUE INDEX ix_vector_spaces_slug ON vector_spaces (slug)")

    require_fresh_database(f"sqlite+aiosqlite:///{path}")
    upgrade_database(f"sqlite+aiosqlite:///{path}")

    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert "vector_spaces" not in tables
    assert revision == ("0006_agent_backends",)


def test_grpc_server_columns_are_removed_without_losing_settings(
    tmp_path: Path,
) -> None:
    from alembic import command
    from alembic.config import Config
    import sqlite3

    import audio_harness.database.session as database_session

    path = tmp_path / "grpc-server.db"
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(database_session.__file__).with_name("migrations")),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "0003_remove_vector_spaces")

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE workflow_settings SET min_chunk_bytes = 800, "
            "grpc_audio_workflow_id = 'default-audio' WHERE id = 1"
        )
        connection.execute(
            "UPDATE capacity_settings SET realtime_max_sessions = 31, "
            "grpc_max_sessions = 7 WHERE id = 1"
        )

    require_fresh_database(f"sqlite+aiosqlite:///{path}")
    upgrade_database(f"sqlite+aiosqlite:///{path}")

    with sqlite3.connect(path) as connection:
        workflow_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(workflow_settings)")
        }
        capacity_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(capacity_settings)")
        }
        workflow_values = connection.execute(
            "SELECT min_chunk_bytes FROM workflow_settings WHERE id = 1"
        ).fetchone()
        capacity_values = connection.execute(
            "SELECT realtime_max_sessions FROM capacity_settings WHERE id = 1"
        ).fetchone()
    assert "grpc_audio_workflow_id" not in workflow_columns
    assert "grpc_max_sessions" not in capacity_columns
    assert workflow_values == (800,)
    assert capacity_values == (31,)

    command.downgrade(config, "0003_remove_vector_spaces")
    with sqlite3.connect(path) as connection:
        workflow_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(workflow_settings)")
        }
        capacity_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(capacity_settings)")
        }
    assert "grpc_audio_workflow_id" in workflow_columns
    assert "grpc_max_sessions" in capacity_columns
