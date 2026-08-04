from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import grpc
import httpx
import pytest
import pytest_asyncio

from audio_harness.api.service import EngineCatalog
from audio_harness.capacity import ServingCapacity
from audio_harness.config import (
    AppConfig,
    CapacityConfig,
    WorkflowRuntimeConfig,
)
from audio_harness.database import Database, upgrade_database
from audio_harness.engines.router import EngineRouter
from audio_harness.inferencers.grpc import GrpcInferencerFactory
from audio_harness.protos.asr import ux_speech_pb2 as pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as pb2_grpc
from audio_harness.pipelines.dag import default_node_registry
from audio_harness.pipelines.service import PipelineStore
from audio_harness.serving.grpc import SpeechServicer
from audio_harness.serving.http import create_http_app
from audio_harness.serving.webrtc import WebRtcCallAnswer
from audio_harness.sessions import TranscriptionSessionService
from audio_harness.workflow_settings import WorkflowSettingsStore


@dataclass
class ControlPlane:
    database: Database
    router: EngineRouter
    catalog: EngineCatalog
    pipeline_store: PipelineStore
    workflow_settings: WorkflowSettingsStore
    client: httpx.AsyncClient


@pytest_asyncio.fixture
async def control_plane(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    upgrade_database(database_url)
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
    pipeline_store = PipelineStore(
        database.session_factory,
        engine_router=router,
        registry=default_node_registry(),
    )
    await pipeline_store.initialize()
    workflow_settings = WorkflowSettingsStore(
        database.session_factory,
        max_message_bytes=1024 * 1024,
    )
    await workflow_settings.initialize()
    app = create_http_app(
        database=database,
        catalog=catalog,
        engine_router=router,
        pipeline_store=pipeline_store,
        workflow_settings=workflow_settings,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield ControlPlane(
            database,
            router,
            catalog,
            pipeline_store,
            workflow_settings,
            client,
        )
    await router.close()
    await database.close()


@pytest_asyncio.fixture
async def reachable_grpc_target():
    server = grpc.aio.server()
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(0)


def engine_payload(
    *,
    slug: str,
    api: str,
    kind: str,
    target: str,
    enabled: bool = True,
    connect_timeout_seconds: float = 0.2,
) -> dict:
    return {
        "slug": slug,
        "name": slug,
        "kind": kind,
        "api": api,
        "protocol": "grpc",
        "target": target,
        "connect_timeout_seconds": connect_timeout_seconds,
        "enabled": enabled,
        "status": "confirmed_running",
    }


@pytest.mark.asyncio
async def test_migration_seeds_assets_and_unconfigured_pipeline(
    control_plane: ControlPlane,
) -> None:
    response = await control_plane.client.get("/api/v1/engines?limit=200")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 11
    assert len(body["items"]) == 11
    assert sum(item["enabled"] for item in body["items"]) == 8
    assert {item["kind"] for item in body["items"]} == {"asr", "vad"}
    assert sum(item["kind"] == "asr" for item in body["items"]) == 5

    pipeline = await control_plane.client.get("/api/v1/pipeline")
    assert pipeline.status_code == 200
    assert pipeline.json()["active_revision"] is None
    definition = pipeline.json()["definition"]
    assert definition["schema_version"] == 2
    stages = definition["stages"]
    assert stages["physical_vad"]["engine_id"] is None
    assert stages["asr"]["engine_id"] is None
    assert stages["semantic_vad"] is None
    assert (
        await control_plane.client.get("/api/v1/engine-bindings")
    ).status_code == 404

    ready = await control_plane.client.get("/health/ready")
    assert ready.status_code == 503
    assert ready.json()["data_plane"] == "not_ready"

    workflow = await control_plane.client.get("/api/v1/workflow-settings")
    assert workflow.status_code == 200
    assert workflow.json()["min_chunk_bytes"] == 640
    assert workflow.json()["revision"] == 1
    capacity = await control_plane.client.get("/api/v1/capacity-settings")
    assert capacity.status_code == 200
    assert capacity.json()["grpc"]["max_sessions"] == 64
    assert capacity.json()["realtime"]["max_sessions"] == 64
    assert capacity.json()["webrtc"]["max_sessions"] == 64
    assert capacity.json()["max_responses_concurrency"] == 8
    assert capacity.json()["max_tts_concurrency"] == 1

    workflows = await control_plane.client.get("/api/v1/workflows")
    assert workflows.status_code == 200
    assert [item["id"] for item in workflows.json()] == [
        "default-transcription"
    ]
    routes = await control_plane.client.get("/api/v1/model-routes")
    assert routes.status_code == 200
    assert {
        item["model"]: (item["workflow_id"], item["visibility"])
        for item in routes.json()
    } == {
        "default-transcription": ("default-transcription", "public"),
        "grpc-default": ("default-transcription", "internal"),
    }


@pytest.mark.asyncio
async def test_manual_engine_validation_records_results_and_reprobes(
    control_plane: ControlPlane,
) -> None:
    existing = await control_plane.client.get("/api/v1/engines?limit=200")
    for engine in existing.json()["items"]:
        disabled = await control_plane.client.patch(
            f"/api/v1/engines/{engine['id']}",
            json={"enabled": False},
        )
        assert disabled.status_code == 200

    server = grpc.aio.server()
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        reachable = await control_plane.client.post(
            "/api/v1/engines",
            json=engine_payload(
                slug="validation-reachable",
                api="ux_speech",
                kind="asr",
                target=f"127.0.0.1:{port}",
            ),
        )
        unreachable = await control_plane.client.post(
            "/api/v1/engines",
            json=engine_payload(
                slug="validation-unreachable",
                api="ux_vad",
                kind="vad",
                target="127.0.0.1:1",
                connect_timeout_seconds=0.05,
            ),
        )
        assert reachable.status_code == 201
        assert unreachable.status_code == 201

        validated = await control_plane.client.post("/api/v1/engines/validate")
        assert validated.status_code == 200
        outcomes = {
            item["engine_id"]: item
            for item in validated.json()["results"]
        }
        assert outcomes[reachable.json()["id"]]["outcome"] == "passed"
        assert outcomes[unreachable.json()["id"]]["outcome"] == "failed"
        assert outcomes[unreachable.json()["id"]]["message"]

        checked_reachable = await control_plane.client.get(
            f"/api/v1/engines/{reachable.json()['id']}"
        )
        checked_unreachable = await control_plane.client.get(
            f"/api/v1/engines/{unreachable.json()['id']}"
        )
        assert checked_reachable.json()["status"] == "healthy"
        assert checked_reachable.json()["revision"] == 1
        assert checked_reachable.json()["status_observed_at"] is not None
        assert checked_unreachable.json()["status"] == "unhealthy"
        assert "人工连通性校验失败" in checked_unreachable.json()["status_basis"]

        await server.stop(0)
        revalidated = await control_plane.client.post("/api/v1/engines/validate")
        outcomes = {
            item["engine_id"]: item
            for item in revalidated.json()["results"]
        }
        assert outcomes[reachable.json()["id"]]["outcome"] == "failed"
    finally:
        await server.stop(0)


@pytest.mark.asyncio
async def test_workflow_settings_update_and_revision_conflict(
    control_plane: ControlPlane,
) -> None:
    current = (
        await control_plane.client.get("/api/v1/workflow-settings")
    ).json()
    current_payload = {
        key: value
        for key, value in current.items()
        if key != "updated_at"
    }
    updated = await control_plane.client.put(
        "/api/v1/workflow-settings",
        json={
            **current_payload,
            "min_chunk_bytes": 800,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["min_chunk_bytes"] == 800
    assert updated.json()["revision"] == 2

    stale = await control_plane.client.put(
        "/api/v1/workflow-settings",
        json={**current_payload, "min_chunk_bytes": 900},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "workflow_revision_conflict"

    updated_payload = {
        key: value
        for key, value in updated.json().items()
        if key != "updated_at"
    }
    invalid = await control_plane.client.put(
        "/api/v1/workflow-settings",
        json={
            **updated_payload,
            "min_chunk_bytes": 801,
        },
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_capacity_settings_update_and_revision_conflict(
    control_plane: ControlPlane,
) -> None:
    current = (
        await control_plane.client.get("/api/v1/capacity-settings")
    ).json()
    payload = {
        key: value
        for key, value in current.items()
        if key != "updated_at"
    }
    updated = await control_plane.client.put(
        "/api/v1/capacity-settings",
        json={
            **payload,
            "grpc": {"max_sessions": 12},
            "realtime": {"max_sessions": 24},
            "webrtc": {"max_sessions": 18},
            "max_asr_concurrency": 3,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["grpc"]["max_sessions"] == 12
    assert updated.json()["realtime"]["max_sessions"] == 24
    assert updated.json()["webrtc"]["max_sessions"] == 18
    assert updated.json()["max_asr_concurrency"] == 3
    assert updated.json()["revision"] == 2

    stale = await control_plane.client.put(
        "/api/v1/capacity-settings",
        json=payload,
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "capacity_revision_conflict"


@pytest.mark.asyncio
async def test_webrtc_calls_http_contract(
    control_plane: ControlPlane,
) -> None:
    class FakeWebRtcService:
        offer_sdp = ""
        session = None
        diagnostics_enabled = False
        hung_up = ""

        async def create_call(
            self,
            *,
            offer_sdp,
            session,
            diagnostics_enabled=False,
        ):
            self.offer_sdp = offer_sdp
            self.session = session
            self.diagnostics_enabled = diagnostics_enabled
            return WebRtcCallAnswer(call_id="call_test", sdp="answer-sdp")

        async def hangup(self, call_id: str) -> None:
            self.hung_up = call_id

    service = FakeWebRtcService()
    app = create_http_app(
        database=control_plane.database,
        catalog=control_plane.catalog,
        engine_router=control_plane.router,
        workflow_settings=control_plane.workflow_settings,
        webrtc_service=service,
    )
    session = {
        "type": "transcription",
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {
                    "model": "gpt-4o-transcribe",
                    "language": "zh-CN",
                },
                "turn_detection": {"type": "server_vad"},
            }
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/v1/realtime/calls",
            params={"diagnostics": "true"},
            files={
                "sdp": (None, "offer-sdp", "application/sdp"),
                "session": (
                    None,
                    json.dumps(session),
                    "application/json",
                ),
            },
        )
        assert created.status_code == 201
        assert created.text == "answer-sdp"
        assert created.headers["location"] == "/v1/realtime/calls/call_test"
        assert service.offer_sdp == "offer-sdp"
        assert service.session.type == "transcription"
        assert service.diagnostics_enabled is True

        hung_up = await client.post(
            "/v1/realtime/calls/call_test/hangup",
        )
        assert hung_up.status_code == 200
        assert service.hung_up == "call_test"


@pytest.mark.asyncio
async def test_engine_crud_filters_and_conflicts(
    control_plane: ControlPlane,
    reachable_grpc_target: str,
) -> None:
    payload = engine_payload(
        slug="test-asr",
        api="ux_speech",
        kind="asr",
        target=reachable_grpc_target,
    )
    created = await control_plane.client.post("/api/v1/engines", json=payload)
    assert created.status_code == 201
    asset_id = created.json()["id"]

    duplicate = await control_plane.client.post(
        "/api/v1/engines",
        json=payload,
    )
    assert duplicate.status_code == 409

    filtered = await control_plane.client.get(
        "/api/v1/engines",
        params={"q": "test-asr", "api": "ux_speech"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1

    patched = await control_plane.client.patch(
        f"/api/v1/engines/{asset_id}",
        json={"description": "测试资产"},
    )
    assert patched.status_code == 200
    assert patched.json()["description"] == "测试资产"
    assert patched.json()["revision"] == 1

    deleted = await control_plane.client.delete(
        f"/api/v1/engines/{asset_id}"
    )
    assert deleted.status_code == 204
    missing = await control_plane.client.get(f"/api/v1/engines/{asset_id}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_asr_vad_kind_is_no_longer_accepted(
    control_plane: ControlPlane,
    reachable_grpc_target: str,
) -> None:
    response = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            slug="legacy-asr-vad",
            api="ux_speech",
            kind="asr_vad",
            target=reachable_grpc_target,
        ),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_pipeline_activation_probes_and_preserves_active_revision(
    control_plane: ControlPlane,
    reachable_grpc_target: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asr = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            slug="reachable-asr",
            api="ux_speech",
            kind="asr",
            target=reachable_grpc_target,
        ),
    )
    vad = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            slug="reachable-vad",
            api="ux_vad",
            kind="vad",
            target=reachable_grpc_target,
        ),
    )
    draft = (
        await control_plane.client.get("/api/v1/pipeline")
    ).json()
    definition = draft["definition"]
    definition["stages"]["asr"]["engine_id"] = asr.json()["id"]
    definition["stages"]["physical_vad"]["engine_id"] = vad.json()["id"]
    saved = await control_plane.client.put(
        "/api/v1/pipeline/draft",
        json={
            "expected_revision": draft["revision"],
            "definition": definition,
        },
    )
    assert saved.status_code == 200
    validation = await control_plane.client.post(
        "/api/v1/pipeline/validate"
    )
    assert validation.json()["valid"] is True
    activated = await control_plane.client.post(
        "/api/v1/pipeline/activate",
        json={"expected_revision": saved.json()["revision"]},
    )
    assert activated.status_code == 200
    assert activated.json()["revision"] == 1
    assert activated.json()["engine_revisions"] == {
        "asr": 1,
        "physical-vad": 1,
    }

    async def reject_engine_readiness(*_args: object) -> None:
        raise AssertionError("健康检查不应探测推理引擎")

    monkeypatch.setattr(
        control_plane.router,
        "readiness",
        reject_engine_readiness,
    )
    ready = await control_plane.client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["data_plane"] == "ready"
    assert ready.json()["nodes"] == []
    assert ready.json()["problems"] == []

    disabled = await control_plane.client.patch(
        f"/api/v1/engines/{asr.json()['id']}",
        json={"enabled": False},
    )
    assert disabled.status_code == 409

    bad = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            slug="unreachable-asr",
            api="ux_speech",
            kind="asr",
            target="127.0.0.1:1",
            connect_timeout_seconds=0.05,
        ),
    )
    failed_definition = saved.json()["definition"]
    failed_definition["stages"]["asr"]["engine_id"] = bad.json()["id"]
    next_draft = await control_plane.client.put(
        "/api/v1/pipeline/draft",
        json={
            "expected_revision": saved.json()["revision"],
            "definition": failed_definition,
        },
    )
    failed = await control_plane.client.post(
        "/api/v1/pipeline/activate",
        json={"expected_revision": next_draft.json()["revision"]},
    )
    assert failed.status_code == 503
    current = await control_plane.client.get("/api/v1/pipeline/active")
    assert current.json()["revision"] == 1

    protected = await control_plane.client.delete(
        f"/api/v1/engines/{asr.json()['id']}"
    )
    assert protected.status_code == 409


@pytest.mark.asyncio
async def test_unbound_data_plane_returns_failed_precondition(
    control_plane: ControlPlane,
) -> None:
    server = grpc.aio.server()
    capacity = ServingCapacity(CapacityConfig())
    sessions = TranscriptionSessionService(
        engine_router=control_plane.router,
        pipeline_store=control_plane.pipeline_store,
        workflow_settings=control_plane.workflow_settings,
        session_limiter=capacity.grpc_sessions,
        asr_limiter=capacity.asr,
    )
    pb2_grpc.add_UxSpeechServicer_to_server(
        SpeechServicer(
            config=AppConfig(
                workflow=WorkflowRuntimeConfig(
                    min_chunk_bytes=4,
                )
            ),
            session_service=sessions,
            workflow_settings=control_plane.workflow_settings,
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb2_grpc.UxSpeechStub(channel)

            async def requests():
                yield pb2.StreamingRecognizeRequest(
                    streaming_config=pb2.StreamingRecognitionConfig(
                        config=pb2.RecognitionConfig(
                            encoding=pb2.RecognitionConfig.LINEAR16,
                            sample_rate_hertz=16000,
                            language_code="zh-CN",
                        )
                    )
                )
                yield pb2.StreamingRecognizeRequest(audio_content=b"\x00\x00")

            with pytest.raises(grpc.aio.AioRpcError) as error:
                await stub.StreamingRecognize(requests()).read()
            assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        await server.stop(0)


@pytest.mark.asyncio
async def test_hot_switch_keeps_existing_lease(
    control_plane: ControlPlane,
) -> None:
    first_server = grpc.aio.server()
    second_server = grpc.aio.server()
    first_port = first_server.add_insecure_port("127.0.0.1:0")
    second_port = second_server.add_insecure_port("127.0.0.1:0")
    await first_server.start()
    await second_server.start()
    try:
        first = await control_plane.client.post(
            "/api/v1/engines",
            json=engine_payload(
                slug="hot-asr-a",
                api="ux_speech",
                kind="asr",
                target=f"127.0.0.1:{first_port}",
            ),
        )
        second = await control_plane.client.post(
            "/api/v1/engines",
            json=engine_payload(
                slug="hot-asr-b",
                api="ux_speech",
                kind="asr",
                target=f"127.0.0.1:{second_port}",
            ),
        )
        vad = await control_plane.client.post(
            "/api/v1/engines",
            json=engine_payload(
                slug="hot-vad",
                api="ux_vad",
                kind="vad",
                target=f"127.0.0.1:{first_port}",
            ),
        )
        draft = (
            await control_plane.client.get("/api/v1/pipeline")
        ).json()
        draft["definition"]["stages"]["asr"]["engine_id"] = first.json()["id"]
        draft["definition"]["stages"]["physical_vad"]["engine_id"] = vad.json()["id"]
        saved = (
            await control_plane.client.put(
                "/api/v1/pipeline/draft",
                json={
                    "expected_revision": draft["revision"],
                    "definition": draft["definition"],
                },
            )
        ).json()
        activated = await control_plane.client.post(
            "/api/v1/pipeline/activate",
            json={"expected_revision": saved["revision"]},
        )
        assert activated.status_code == 200
        ready = await control_plane.client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["data_plane"] == "ready"

        old_plan = control_plane.pipeline_store.current().plan
        async with control_plane.router.lease_snapshots(
            old_plan.engine_snapshots
        ) as old_lease:
            old_asr = old_lease[(first.json()["id"], 1)]
            assert old_asr.endpoint.target.endswith(str(first_port))
            next_definition = saved["definition"]
            next_definition["stages"]["asr"]["engine_id"] = second.json()["id"]
            switched_draft = (
                await control_plane.client.put(
                    "/api/v1/pipeline/draft",
                    json={
                        "expected_revision": saved["revision"],
                        "definition": next_definition,
                    },
                )
            ).json()
            switched = await control_plane.client.post(
                "/api/v1/pipeline/activate",
                json={
                    "expected_revision": switched_draft["revision"]
                },
            )
            assert switched.status_code == 200
            assert old_asr.endpoint.target.endswith(str(first_port))
            new_plan = control_plane.pipeline_store.current().plan
            async with control_plane.router.lease_snapshots(
                new_plan.engine_snapshots
            ) as new_lease:
                new_asr = new_lease[(second.json()["id"], 1)]
                assert new_asr.endpoint.target.endswith(str(second_port))

                updated = await control_plane.client.patch(
                    f"/api/v1/engines/{second.json()['id']}",
                    json={"target": f"127.0.0.1:{first_port}"},
                )
                assert updated.status_code == 200
                assert updated.json()["revision"] == 2
                assert new_asr.endpoint.target.endswith(str(second_port))
                still_ready = await control_plane.client.get(
                    "/health/ready"
                )
                assert still_ready.status_code == 200
                active_before_reactivation = (
                    await control_plane.client.get(
                        "/api/v1/pipeline/active"
                    )
                ).json()
                assert active_before_reactivation[
                    "engine_revisions"
                ]["asr"] == 1

            reactivated = await control_plane.client.post(
                "/api/v1/pipeline/activate",
                json={
                    "expected_revision": switched_draft["revision"]
                },
            )
            assert reactivated.status_code == 200
            assert reactivated.json()["engine_revisions"]["asr"] == 2
            latest_plan = control_plane.pipeline_store.current().plan
            async with control_plane.router.lease_snapshots(
                latest_plan.engine_snapshots
            ) as latest_lease:
                latest_asr = latest_lease[(second.json()["id"], 2)]
                assert latest_asr.endpoint.target.endswith(str(first_port))
    finally:
        await first_server.stop(0)
        await second_server.stop(0)


@pytest.mark.asyncio
async def test_v2_workflow_create_autosave_status_and_public_route(
    control_plane: ControlPlane,
) -> None:
    created = await control_plane.client.post(
        "/api/v1/workflows",
        json={
            "slug": "support-agent",
            "name": "客服语音 Agent",
            "kind": "voice_agent",
        },
    )
    assert created.status_code == 201
    draft = created.json()
    assert draft["workflow_id"]
    assert draft["definition"]["schema_version"] == 2
    assert draft["definition"]["stages"]["responses_agent"] is not None
    assert draft["definition"]["stages"]["duplex_tts"] is not None

    duplicate = await control_plane.client.post(
        "/api/v1/workflows",
        json={
            "slug": "support-agent",
            "name": "重复工作流",
            "kind": "voice_agent",
        },
    )
    assert duplicate.status_code == 409

    workflow_id = draft["workflow_id"]
    routes = (await control_plane.client.get("/api/v1/model-routes")).json()
    assert any(
        route["model"] == "support-agent"
        and route["workflow_id"] == workflow_id
        and route["visibility"] == "public"
        for route in routes
    )

    status_response = await control_plane.client.get(
        f"/api/v1/workflows/{workflow_id}/status"
    )
    assert status_response.status_code == 200
    workflow_status = status_response.json()
    assert workflow_status["state"] == "incomplete"
    assert workflow_status["public_models"] == ["support-agent"]
    assert {stage["stage"] for stage in workflow_status["stages"]} == {
        "physical_vad",
        "asr",
        "responses_agent",
        "duplex_tts",
    }

    saved_definition = draft["definition"]
    saved_definition["stages"]["responses_agent"]["config"]["model"] = (
        "gpt-4.1-mini"
    )
    saved = await control_plane.client.put(
        f"/api/v1/workflows/{workflow_id}/draft",
        json={
            "expected_revision": draft["revision"],
            "definition": saved_definition,
        },
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == draft["revision"] + 1

    conflict = await control_plane.client.put(
        f"/api/v1/workflows/{workflow_id}/draft",
        json={
            "expected_revision": draft["revision"],
            "definition": saved_definition,
        },
    )
    assert conflict.status_code == 409
