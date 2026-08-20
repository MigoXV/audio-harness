from __future__ import annotations

import pytest
import httpx
import pytest_asyncio

from audio_harness.agent_backends.base import AgentBackendGateway
from audio_harness.agent_backends.models import (
    AgentBackendCapabilities,
    AgentBackendPlugin,
    AgentBackendSnapshot,
)
from audio_harness.configs import AppConfig
from audio_harness.database import upgrade_database
from audio_harness.serving.host import AudioHarnessHost


@pytest_asyncio.fixture
async def control_plane(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'agent-backends.db'}"
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.http_app),
        base_url="http://test",
    ) as client:
        yield type("ControlPlane", (), {"host": host, "client": client})()
    await host.stop()


class FakeAgentBackendGateway(AgentBackendGateway):
    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.snapshots: list[AgentBackendSnapshot] = []
        self.retired: list[tuple[str, int | None]] = []

    async def capabilities(
        self,
        snapshot: AgentBackendSnapshot,
    ) -> AgentBackendCapabilities:
        self.snapshots.append(snapshot)
        if self.failure is not None:
            raise self.failure
        return AgentBackendCapabilities(
            wire_version="0.0.1",
            provider="deepseek-official",
            model="deepseek-v4-flash",
            cwd="/workspace",
            presets=("standard", "minimal"),
            plugins=(
                AgentBackendPlugin(
                    entry_id="tool-bash",
                    module_name="@deepseek-ai/dsh-tool-bash",
                    enabled=True,
                    phase="active",
                ),
            ),
        )

    async def open_session(
        self,
        snapshot: AgentBackendSnapshot,
        *,
        cwd: str,
        agent_preset: str,
    ):
        raise NotImplementedError

    async def retire(
        self,
        backend_id: str,
        *,
        before_revision: int | None = None,
    ) -> None:
        self.retired.append((backend_id, before_revision))

    async def close(self) -> None:
        return None


def backend_payload(slug: str = "dsh-local") -> dict:
    return {
        "slug": slug,
        "name": "本地 DSH",
        "kind": "dsh",
        "protocol": "dsh_web_v1",
        "target": "http://127.0.0.1:3080",
        "connect_timeout_seconds": 2,
        "request_timeout_seconds": 30,
        "headers_env": {"Authorization": "DSH_AUTHORIZATION"},
        "enabled": True,
        "description": "测试 DSH",
        "details": {},
    }


@pytest.mark.asyncio
async def test_agent_backend_crud_and_runtime_revision(
    control_plane,
) -> None:
    gateway = FakeAgentBackendGateway()
    control_plane.host.agent_backends._gateway = gateway
    created = await control_plane.client.post(
        "/api/v1/agent-backends",
        json=backend_payload(),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "unknown"
    assert body["revision"] == 1

    listed = await control_plane.client.get("/api/v1/agent-backends")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    metadata = await control_plane.client.patch(
        f"/api/v1/agent-backends/{body['id']}",
        json={"name": "新的显示名"},
    )
    assert metadata.status_code == 200
    assert metadata.json()["revision"] == 1

    runtime = await control_plane.client.patch(
        f"/api/v1/agent-backends/{body['id']}",
        json={"request_timeout_seconds": 45},
    )
    assert runtime.status_code == 200
    assert runtime.json()["revision"] == 2
    assert gateway.retired == [(body["id"], 2)]

    deleted = await control_plane.client.delete(
        f"/api/v1/agent-backends/{body['id']}"
    )
    assert deleted.status_code == 204
    assert gateway.retired[-1] == (body["id"], None)


@pytest.mark.asyncio
async def test_agent_backend_capabilities_and_health_are_persisted(
    control_plane,
) -> None:
    gateway = FakeAgentBackendGateway()
    control_plane.host.agent_backends._gateway = gateway
    created = await control_plane.client.post(
        "/api/v1/agent-backends",
        json=backend_payload("dsh-capabilities"),
    )
    backend_id = created.json()["id"]

    capabilities = await control_plane.client.get(
        f"/api/v1/agent-backends/{backend_id}/capabilities"
    )
    assert capabilities.status_code == 200
    assert capabilities.json()["presets"] == ["standard", "minimal"]
    assert capabilities.json()["plugins"][0]["enabled"] is True
    assert gateway.snapshots[-1].headers_env == {
        "Authorization": "DSH_AUTHORIZATION"
    }

    checked = await control_plane.client.post(
        f"/api/v1/agent-backends/{backend_id}/validate"
    )
    assert checked.status_code == 200
    assert checked.json()["outcome"] == "passed"
    stored = await control_plane.client.get(
        f"/api/v1/agent-backends/{backend_id}"
    )
    assert stored.json()["status"] == "healthy"
    assert stored.json()["last_success_at"] is not None

    gateway.failure = RuntimeError("DSH offline")
    failed = await control_plane.client.post(
        f"/api/v1/agent-backends/{backend_id}/validate"
    )
    assert failed.status_code == 200
    assert failed.json()["outcome"] == "failed"
    stored = await control_plane.client.get(
        f"/api/v1/agent-backends/{backend_id}"
    )
    assert stored.json()["status"] == "unhealthy"
    assert stored.json()["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_agent_backend_validation_and_unavailable_errors(
    control_plane,
) -> None:
    invalid = backend_payload("bad-target")
    invalid["target"] = "ws://127.0.0.1:3080"
    response = await control_plane.client.post(
        "/api/v1/agent-backends",
        json=invalid,
    )
    assert response.status_code == 409

    invalid = backend_payload("bad-header")
    invalid["headers_env"] = {"Host": "DSH_HOST"}
    response = await control_plane.client.post(
        "/api/v1/agent-backends",
        json=invalid,
    )
    assert response.status_code == 422

    duplicate = backend_payload("duplicate")
    assert (
        await control_plane.client.post(
            "/api/v1/agent-backends", json=duplicate
        )
    ).status_code == 201
    assert (
        await control_plane.client.post(
            "/api/v1/agent-backends", json=duplicate
        )
    ).status_code == 409


@pytest.mark.asyncio
async def test_disabled_agent_backend_rejects_live_operations(
    control_plane,
) -> None:
    gateway = FakeAgentBackendGateway()
    control_plane.host.agent_backends._gateway = gateway
    payload = backend_payload("disabled")
    payload["enabled"] = False
    created = await control_plane.client.post(
        "/api/v1/agent-backends", json=payload
    )
    backend_id = created.json()["id"]
    assert created.json()["status"] == "disabled"
    assert (
        await control_plane.client.post(
            f"/api/v1/agent-backends/{backend_id}/validate"
        )
    ).status_code == 409
    assert (
        await control_plane.client.get(
            f"/api/v1/agent-backends/{backend_id}/capabilities"
        )
    ).status_code == 409
