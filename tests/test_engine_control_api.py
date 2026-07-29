from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import grpc
import httpx
import pytest
import pytest_asyncio

from audio_harness.api import create_management_app
from audio_harness.api.service import EngineCatalog
from audio_harness.config import AppConfig, WorkflowConfig
from audio_harness.database import Database, upgrade_database
from audio_harness.engines.router import EngineRouter
from audio_harness.inferencers.grpc import GrpcInferencerFactory
from audio_harness.protos.asr import ux_speech_pb2 as pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as pb2_grpc
from audio_harness.servicers.speech import SpeechServicer


@dataclass
class ControlPlane:
    database: Database
    router: EngineRouter
    catalog: EngineCatalog
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
    app = create_management_app(
        database=database,
        catalog=catalog,
        engine_router=router,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield ControlPlane(database, router, catalog, client)
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
async def test_migration_seeds_all_assets_without_bindings(
    control_plane: ControlPlane,
) -> None:
    response = await control_plane.client.get("/api/v1/engines?limit=200")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 11
    assert len(body["items"]) == 11
    assert sum(item["enabled"] for item in body["items"]) == 8

    bindings = (
        await control_plane.client.get("/api/v1/engine-bindings")
    ).json()
    assert [item["role"] for item in bindings] == [
        "asr",
        "vad",
        "semantic_vad",
    ]
    assert all(item["engine"] is None for item in bindings)

    ready = await control_plane.client.get("/health/ready")
    assert ready.status_code == 503
    assert ready.json()["data_plane"] == "not_ready"


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
async def test_binding_probes_and_preserves_old_binding_on_failure(
    control_plane: ControlPlane,
    reachable_grpc_target: str,
) -> None:
    good = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            slug="reachable-asr",
            api="ux_speech",
            kind="asr",
            target=reachable_grpc_target,
        ),
    )
    good_id = good.json()["id"]
    bound = await control_plane.client.put(
        "/api/v1/engine-bindings/asr",
        json={"engine_id": good_id},
    )
    assert bound.status_code == 200
    assert bound.json()["engine"]["id"] == good_id

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
    failed = await control_plane.client.put(
        "/api/v1/engine-bindings/asr",
        json={"engine_id": bad.json()["id"]},
    )
    assert failed.status_code == 503
    current = await control_plane.client.get(
        "/api/v1/engine-bindings/asr"
    )
    assert current.json()["engine"]["id"] == good_id

    incompatible = await control_plane.client.post(
        "/api/v1/engines",
        json=engine_payload(
            slug="vad-for-asr",
            api="ux_vad",
            kind="vad",
            target=reachable_grpc_target,
        ),
    )
    response = await control_plane.client.put(
        "/api/v1/engine-bindings/asr",
        json={"engine_id": incompatible.json()["id"]},
    )
    assert response.status_code == 409

    protected = await control_plane.client.delete(
        f"/api/v1/engines/{good_id}"
    )
    assert protected.status_code == 409


@pytest.mark.asyncio
async def test_unbound_data_plane_returns_failed_precondition(
    control_plane: ControlPlane,
) -> None:
    server = grpc.aio.server()
    pb2_grpc.add_UxSpeechServicer_to_server(
        SpeechServicer(
            config=AppConfig(
                workflow=WorkflowConfig(
                    min_chunk_bytes=4,
                    min_segment_seconds=0,
                )
            ),
            engine_provider=control_plane.router,
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
        await control_plane.client.put(
            "/api/v1/engine-bindings/asr",
            json={"engine_id": first.json()["id"]},
        )
        await control_plane.client.put(
            "/api/v1/engine-bindings/vad",
            json={"engine_id": vad.json()["id"]},
        )
        ready = await control_plane.client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["data_plane"] == "ready"

        async with control_plane.router.lease() as old_lease:
            assert old_lease.asr.endpoint.target.endswith(str(first_port))
            switched = await control_plane.client.put(
                "/api/v1/engine-bindings/asr",
                json={"engine_id": second.json()["id"]},
            )
            assert switched.status_code == 200
            assert old_lease.asr.endpoint.target.endswith(str(first_port))
            async with control_plane.router.lease() as new_lease:
                assert new_lease.asr.endpoint.target.endswith(str(second_port))

        async with control_plane.router.lease() as pinned_lease:
            updated = await control_plane.client.patch(
                f"/api/v1/engines/{second.json()['id']}",
                json={"target": f"127.0.0.1:{first_port}"},
            )
            assert updated.status_code == 200
            assert updated.json()["revision"] == 2
            assert pinned_lease.asr.endpoint.target.endswith(str(second_port))
            async with control_plane.router.lease() as updated_lease:
                assert updated_lease.asr.endpoint.target.endswith(
                    str(first_port)
                )

        disabled = await control_plane.client.patch(
            f"/api/v1/engines/{second.json()['id']}",
            json={"enabled": False},
        )
        assert disabled.status_code == 409
    finally:
        await first_server.stop(0)
        await second_server.stop(0)
