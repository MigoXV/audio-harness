from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import grpc
import pytest
from fastapi import FastAPI, WebSocket

from audio_harness.configs import AppConfig, CapacityConfig, WorkflowRuntimeConfig
from audio_harness.protos.aha_grpc.v1 import aha_realtime_pb2 as pb
from audio_harness.protos.aha_grpc.v1 import aha_realtime_pb2_grpc as rpc
from audio_harness.serving.aha_grpc.codec import (
    decode_client_event,
    decode_server_event,
    encode_client_event,
    encode_server_event,
)
from audio_harness.serving.aha_grpc.schema import generate
from audio_harness.serving.aha_grpc.server import AhaGrpcServer
from audio_harness.serving.capacity import ServingCapacity
from audio_harness.serving.realtime.connection import RealtimeTranscriptionService
from audio_harness.serving.realtime.core import RealtimeOptions
from audio_harness.serving.realtime.json_codec import decode_client_event as decode_ws
from audio_harness.sessions.audio_workflow import AudioWorkflowSessionService
from tests.graph_fakes import StaticRouteService, static_audio_workflow
from tests.test_realtime_service import FinalAsr, RecordingVad, StaticSettings


def test_schema_covers_current_protocol_and_keeps_field_numbers() -> None:
    generate(check=True)
    assert len(pb.ClientEvent.DESCRIPTOR.oneofs_by_name["event"].fields) == 14
    assert len(pb.ServerEvent.DESCRIPTOR.oneofs_by_name["event"].fields) == 77


@pytest.mark.parametrize(
    "input_config",
    [
        {},
        {"turn_detection": None},
        {"turn_detection": {"type": "server_vad", "create_response": False}},
        {"format": {"type": "audio/pcm", "rate": 16000}},
    ],
)
def test_session_update_preserves_presence(input_config) -> None:
    payload = {
        "type": "session.update",
        "event_id": "cfg",
        "session": {"type": "transcription", "audio": {"input": input_config}},
    }
    assert decode_client_event(encode_client_event(payload)) == payload


@pytest.mark.parametrize(
    "tools",
    [
        None,
        [],
        [
            {
                "type": "function",
                "name": "test",
                "parameters": {
                    "type": "object",
                    "properties": {"n": {"const": 2**60 + 1}},
                },
            }
        ],
    ],
)
def test_tools_and_arbitrary_json_preserve_null_empty_and_integers(tools) -> None:
    payload = {
        "type": "session.update",
        "session": {"type": "realtime", "tools": tools},
    }
    assert decode_client_event(encode_client_event(payload)) == payload


def test_binary_audio_and_nested_audio_are_not_base64() -> None:
    pcm = b"\x00\xff\x01\x80"
    payload = {"type": "input_audio_buffer.append", "audio": pcm}
    encoded = encode_client_event(payload)
    assert encoded.input_audio_buffer_append.audio == pcm
    assert decode_client_event(encoded) == payload
    payload = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_audio", "audio": pcm}],
        },
    }
    assert decode_client_event(encode_client_event(payload)) == payload
    server = {
        "type": "response.output_audio.delta",
        "event_id": "e",
        "response_id": "r",
        "item_id": "i",
        "content_index": 0,
        "output_index": 0,
        "delta": pcm,
    }
    assert encode_server_event(server).response_output_audio_delta.delta == pcm
    assert decode_server_event(encode_server_event(server)) == server


def test_cluster_snapshot_preserves_explicit_null_and_negative_speaker() -> None:
    payload = {
        "type": "x_aha.diarization.cluster.completed",
        "event_id": "e",
        "revision": 1,
        "replaces_revision": None,
        "full_snapshot": True,
        "final": True,
        "processed_audio_ms": 0,
        "assignments": [{"segment_id": "s", "local_speaker_id": "l", "speaker_id": -2}],
    }
    assert decode_server_event(encode_server_event(payload)) == payload
    payload["assignments"] = []
    assert decode_server_event(encode_server_event(payload)) == payload


def test_rejects_mismatched_event_and_unset_union() -> None:
    message = pb.ClientEvent()
    message.input_audio_buffer_clear.type = "input_audio_buffer.commit"
    with pytest.raises(ValueError, match="does not match"):
        decode_client_event(message)
    with pytest.raises(ValueError, match="Exactly one"):
        decode_client_event(pb.ClientEvent())
    message = pb.ClientEvent()
    message.session_update.type = "session.update"
    message.session_update.session.SetInParent()
    with pytest.raises(ValueError, match="kind is required"):
        decode_client_event(message)


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory):
    directory = tmp_path_factory.mktemp("aha-grpc-tls")
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def make_service(*, max_sessions=64, queue_capacity=32):
    settings = StaticSettings(
        WorkflowRuntimeConfig(min_chunk_bytes=640, result_queue_capacity=queue_capacity)
    )
    capacity = ServingCapacity(CapacityConfig(realtime={"max_sessions": max_sessions}))
    pool, _, workflow = static_audio_workflow(asr=FinalAsr(), vad=RecordingVad())
    sessions = AudioWorkflowSessionService(
        inferencer_pool=pool,
        workflow_settings=settings,
        session_limiter=capacity.realtime_sessions,
        asr_limiter=capacity.asr,
    )
    service = RealtimeTranscriptionService(
        session_service=sessions,
        workflow_settings=settings,
        models=StaticRouteService(workflow),
        max_message_bytes=1024 * 1024,
        inferencer_pool=pool,
    )
    return service, pool, capacity


@asynccontextmanager
async def running_server(tls_files, *, service=None):
    cert, key = tls_files
    if service is None:
        service, _, _ = make_service()
    config = AppConfig.model_validate(
        {
            "http": {"tls_cert_file": cert, "tls_key_file": key},
            "aha_grpc": {"host": "127.0.0.1", "port": 0},
        }
    )
    server = AhaGrpcServer(config, service)
    await server.start()
    try:
        async with grpc.aio.secure_channel(
            f"localhost:{server.port}", grpc.ssl_channel_credentials(cert.read_bytes())
        ) as channel:
            yield rpc.AhaRealtimeStub(channel), service, server
    finally:
        await server.stop()


def session_update(*, manual=True):
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 16000},
                    "transcription": {"model": "gpt-4o-transcribe"},
                    "turn_detection": None if manual else {"type": "server_vad"},
                }
            },
        },
    }


async def read_event(call):
    event = await call.read()
    assert event is not grpc.aio.EOF
    return decode_server_event(event)


async def read_remaining(call):
    events = []
    while True:
        message = await call.read()
        if message is grpc.aio.EOF:
            return events
        events.append(decode_server_event(message))


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [True, False])
@pytest.mark.parametrize("explicit_commit", [True, False])
async def test_tls_stream_drains_tail_without_duplicate_commit(
    tls_files, manual, explicit_commit
):
    service, pool, _ = make_service(queue_capacity=1)
    async with running_server(tls_files, service=service) as (stub, _, _):
        call = stub.Connect(metadata=(("intent", "transcription"),), timeout=10)
        assert (await read_event(call))["type"] == "session.created"
        await call.write(encode_client_event(session_update(manual=manual)))
        assert (await read_event(call))["type"] == "session.updated"
        # Less than min_chunk_bytes: EOF must flush even if no workflow started yet.
        await call.write(
            encode_client_event(
                {"type": "input_audio_buffer.append", "audio": b"\x00\x00" * 100}
            )
        )
        if explicit_commit:
            await call.write(encode_client_event({"type": "input_audio_buffer.commit"}))
        await call.done_writing()
        received = await read_remaining(call)
        kinds = [e["type"] for e in received]
        assert kinds.count("input_audio_buffer.committed") == 1
        assert kinds.count("conversation.item.input_audio_transcription.completed") == 1
        assert "error" not in kinds
        assert any(
            e["type"] == "x_aha.vad.progress" and e.get("final") for e in received
        )
        assert await call.code() is grpc.StatusCode.OK
    assert pool.active_leases == 0


@pytest.mark.asyncio
async def test_business_errors_are_recoverable(tls_files):
    async with running_server(tls_files) as (stub, _, _):
        call = stub.Connect(metadata=(("intent", "transcription"),), timeout=10)
        await read_event(call)
        await call.write(pb.ClientEvent())
        assert (await read_event(call))["error"]["code"] == "invalid_event"
        await call.write(encode_client_event(session_update()))
        assert (await read_event(call))["type"] == "session.updated"
        await call.write(
            encode_client_event(
                {"type": "input_audio_buffer.append", "event_id": "bad", "audio": b"1"}
            )
        )
        error = await read_event(call)
        assert error["error"]["code"] == "invalid_audio"
        assert error["error"]["event_id"] == "bad"
        await call.done_writing()
        assert await call.read() is grpc.aio.EOF


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        (),
        (("model", "m"), ("intent", "transcription")),
        (("intent", "wrong"),),
        (("intent", "transcription"), ("x_aha", "v2")),
    ],
)
async def test_invalid_metadata_rejects_connection(tls_files, metadata):
    async with running_server(tls_files) as (stub, _, _):
        call = stub.Connect(metadata=metadata, timeout=5)
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await call.read()
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_disconnect_and_deadline_release_workflow(tls_files):
    service, pool, _ = make_service()
    async with running_server(tls_files, service=service) as (stub, _, _):
        for cancel in (True, False):
            call = stub.Connect(
                metadata=(("intent", "transcription"),),
                timeout=0.5 if not cancel else 5,
            )
            await read_event(call)
            await call.write(encode_client_event(session_update(manual=False)))
            await read_event(call)
            await call.write(
                encode_client_event(
                    {"type": "input_audio_buffer.append", "audio": b"\x00\x00" * 640}
                )
            )
            if cancel:
                call.cancel()
            else:
                with pytest.raises(grpc.aio.AioRpcError):
                    while await call.read() is not grpc.aio.EOF:
                        pass
            for _ in range(100):
                if pool.active_leases == 0:
                    break
                await asyncio.sleep(0.01)
            assert pool.active_leases == 0


@pytest.mark.asyncio
async def test_ws_and_grpc_use_identical_core_events(tls_files):
    service, _, _ = make_service()
    # Exercise the WS codec through the exact same service entrypoint, then
    # compare against an actual TLS gRPC stream with identical application input.
    connection = await service.open(
        RealtimeOptions(intent="transcription", aha_version="v1")
    )
    expected = []

    async def consume():
        async for event in connection.events():
            expected.append(event)

    task = asyncio.create_task(consume())
    try:
        for payload in [
            session_update(),
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(b"\x00\x00" * 100).decode(),
            },
            {"type": "input_audio_buffer.commit"},
        ]:
            await connection.handle_event(decode_ws(payload))
        await connection.drain_events()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await connection.close()
    async with running_server(tls_files) as (stub, _, _):
        call = stub.Connect(metadata=(("intent", "transcription"),), timeout=5)
        await call.write(encode_client_event(session_update()))
        await call.write(
            encode_client_event(
                {"type": "input_audio_buffer.append", "audio": b"\x00\x00" * 100}
            )
        )
        await call.write(encode_client_event({"type": "input_audio_buffer.commit"}))
        await call.done_writing()
        actual = await read_remaining(call)

    def normalize(events):
        ids = {}

        def visit(value, key=""):
            if isinstance(value, dict):
                return {
                    k: visit(v, k)
                    for k, v in sorted(value.items())
                    if k != "latency_ms"
                }
            if isinstance(value, list):
                return [visit(v, key) for v in value]
            if isinstance(value, str) and (key.endswith("_id") or key == "id"):
                return ids.setdefault(value, len(ids))
            return value

        return visit(events)

    assert normalize(actual) == normalize(expected)


@pytest.mark.asyncio
async def test_agent_binary_audio_response_drains_after_half_close(tls_files):
    from tests.test_agent_runtime import FakePool, _profile

    service, _, _ = make_service()

    class Models:
        async def resolve(self, model):
            assert model == "voice-agent"
            return _profile()

    service.models = Models()
    pool = FakePool()
    service.inferencer_pool = pool
    async with running_server(tls_files, service=service) as (stub, _, _):
        call = stub.Connect(metadata=(("model", "voice-agent"),), timeout=10)
        assert (await read_event(call))["type"] == "session.created"
        await call.write(
            encode_client_event(
                {
                    "type": "response.create",
                    "response": {
                        "input": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "你好"}],
                            }
                        ]
                    },
                }
            )
        )
        await call.done_writing()
        events = await read_remaining(call)
        assert not [e for e in events if e["type"] == "error"]
        audio = [
            e["delta"] for e in events if e["type"] == "response.output_audio.delta"
        ]
        assert audio and all(isinstance(pcm, bytes) for pcm in audio)
        assert events[-1]["type"] == "response.done"
        assert events[-1]["response"]["status"] == "completed"
        assert pool.tts.calls == 1


@pytest.mark.asyncio
async def test_half_closed_agent_does_not_wait_for_client_tool(tls_files):
    from tests.test_agent_runtime import ClientToolResponses, FakePool, _profile

    service, _, _ = make_service()

    class Models:
        async def resolve(self, model):
            return _profile()

    service.models = Models()
    pool = FakePool()
    pool.responses = ClientToolResponses()
    service.inferencer_pool = pool
    async with running_server(tls_files, service=service) as (stub, _, _):
        call = stub.Connect(metadata=(("model", "voice-agent"),), timeout=5)
        await read_event(call)
        await call.write(
            encode_client_event(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "output_modalities": ["text"],
                        "tools": [
                            {
                                "type": "function",
                                "name": "client_tool",
                                "parameters": {"type": "object"},
                            }
                        ],
                    },
                }
            )
        )
        await read_event(call)
        await call.write(encode_client_event({"type": "response.create"}))
        await call.done_writing()
        events = await read_remaining(call)
        assert any(
            e["type"] == "error" and e["error"]["code"] == "client_input_closed"
            for e in events
        )
        assert events[-1]["type"] == "response.done"
        assert events[-1]["response"]["status"] == "failed"


@pytest.mark.asyncio
async def test_tls_listener_rejects_missing_cert_and_occupied_port(tls_files):
    service, _, _ = make_service()
    with pytest.raises(ValueError, match="TLS"):
        await AhaGrpcServer(AppConfig(), service).start()
    async with running_server(tls_files) as (_, _, first):
        config = first.config.model_copy(
            update={
                "aha_grpc": first.config.aha_grpc.model_copy(
                    update={"port": first.port}
                )
            }
        )
        second = AhaGrpcServer(config, service)
        with pytest.raises(RuntimeError):
            await second.start()
        assert second._server is None


@pytest.mark.asyncio
async def test_shared_capacity_is_enforced_and_released(tls_files):
    service, pool, _ = make_service(max_sessions=1)
    async with running_server(tls_files, service=service) as (stub, _, _):
        first = stub.Connect(metadata=(("intent", "transcription"),), timeout=5)
        await read_event(first)
        await first.write(encode_client_event(session_update(manual=False)))
        await read_event(first)
        await first.write(
            encode_client_event(
                {"type": "input_audio_buffer.append", "audio": b"\0\0" * 640}
            )
        )
        for _ in range(100):
            if pool.active_leases:
                break
            await asyncio.sleep(0.01)
        assert pool.active_leases == 1
        second = stub.Connect(metadata=(("intent", "transcription"),), timeout=5)
        await read_event(second)
        await second.write(encode_client_event(session_update(manual=False)))
        await read_event(second)
        await second.write(
            encode_client_event(
                {"type": "input_audio_buffer.append", "audio": b"\0\0" * 640}
            )
        )
        with pytest.raises(grpc.aio.AioRpcError) as failure:
            while await second.read() is not grpc.aio.EOF:
                pass
        assert failure.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        first.cancel()
    assert pool.active_leases == 0


def test_every_protocol_event_has_a_lossless_minimal_wire_representation():
    from audio_harness.serving.aha_grpc.codec import (
        _schema,
        _encode_message,
        _decode_message,
    )

    schema = _schema()

    def sample(value):
        if "$ref" in value:
            return sample(schema["definitions"][value["$ref"].rsplit("/", 1)[-1]])
        if "const" in value:
            return value["const"]
        if "enum" in value:
            return value["enum"][0]
        if "anyOf" in value:
            return sample(value["anyOf"][0])
        kind = value.get("type")
        if kind == "object":
            return {
                k: sample(value["properties"][k]) for k in value.get("required", [])
            }
        if kind == "array":
            return []
        if kind == "string":
            return "test"
        if kind == "boolean":
            return False
        if kind in ("number", "integer"):
            return max(value.get("minimum", 0), value.get("exclusiveMinimum", -1) + 1)
        return None

    for envelope in ("ClientEvent", "ServerEvent"):
        for event_type, spec in schema["messages"][envelope]["events"].items():
            definition = next(
                v for k, v in schema["definitions"].items() if k == spec["proto"]
            )
            payload = sample(definition)
            payload["type"] = event_type
            if event_type == "input_audio_buffer.append":
                payload["audio"] = b"\x00\xff"
            if event_type == "response.output_audio.delta":
                payload["delta"] = b"\x00\xff"
            message = getattr(pb, envelope)()
            _encode_message(message, payload)
            assert _decode_message(message) == payload, event_type


@pytest.mark.asyncio
async def test_python_client_and_actual_ws_produce_matching_transcription(tls_files):
    from fastapi import FastAPI, WebSocket
    from fastapi.testclient import TestClient
    from audio_harness.serving.aha_grpc.client import AhaGrpcClient

    def run_ws():
        app = FastAPI()
        service, _, _ = make_service()

        @app.websocket("/v1/realtime")
        async def endpoint(websocket: WebSocket):
            await service.handle(websocket, aha_version="v1")

        with TestClient(app) as client:
            with client.websocket_connect("/v1/realtime") as ws:
                ws.receive_json()
                ws.send_json(session_update())
                ws.receive_json()
                ws.send_json(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(b"\0\0" * 100).decode(),
                    }
                )
                ws.send_json({"type": "input_audio_buffer.commit"})
                while True:
                    event = ws.receive_json()
                    if (
                        event["type"]
                        == "conversation.item.input_audio_transcription.completed"
                    ):
                        return event["transcript"]

    expected = await asyncio.to_thread(run_ws)
    async with running_server(tls_files) as (_, _, server):
        cert, _ = tls_files
        async with grpc.aio.secure_channel(
            f"localhost:{server.port}", grpc.ssl_channel_credentials(cert.read_bytes())
        ) as channel:
            async with AhaGrpcClient(channel).connect(
                intent="transcription", timeout=5
            ) as connection:
                events = []

                async def consume():
                    async for event in connection.events():
                        events.append(event)

                reader = asyncio.create_task(consume())
                try:
                    await connection.send(session_update())
                    await connection.send(
                        {"type": "input_audio_buffer.append", "audio": b"\0\0" * 100}
                    )
                    await connection.finish_input()
                    await reader
                finally:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                transcripts = [
                    e["transcript"]
                    for e in events
                    if e["type"]
                    == "conversation.item.input_audio_transcription.completed"
                ]
                assert transcripts == [expected]


@pytest.mark.asyncio
async def test_host_always_starts_grpc_with_existing_tls(tls_files, tmp_path):
    from audio_harness.database import upgrade_database
    from audio_harness.serving.host import AudioHarnessHost

    cert, key = tls_files
    config = AppConfig.model_validate(
        {
            "database": {"url": f'sqlite+aiosqlite:///{tmp_path / "host.db"}'},
            "http": {"tls_cert_file": cert, "tls_key_file": key},
            "aha_grpc": {"host": "127.0.0.1", "port": 0},
            "voices": {"storage_dir": tmp_path / "voices"},
        }
    )
    upgrade_database(config.database.url)
    host = AudioHarnessHost(config)
    await host.start()
    try:
        async with grpc.aio.secure_channel(
            f"localhost:{host.grpc_server.port}",
            grpc.ssl_channel_credentials(cert.read_bytes()),
        ) as channel:
            call = rpc.AhaRealtimeStub(channel).Connect(
                metadata=(("intent", "transcription"),), timeout=5
            )
            assert (await read_event(call))["type"] == "session.created"
            await call.done_writing()
            assert await call.read() is grpc.aio.EOF
    finally:
        await host.stop()
    assert host.grpc_server._server is None


def test_grpc_environment_and_cli_overrides(monkeypatch):
    from audio_harness.commands.serve import apply_serve_overrides

    monkeypatch.setenv("AHA_GRPC_HOST", "127.0.0.2")
    monkeypatch.setenv("AHA_GRPC_PORT", "50061")
    config = AppConfig.from_environment()
    assert config.aha_grpc.host == "127.0.0.2"
    assert config.aha_grpc.port == 50061
    updated = apply_serve_overrides(
        config,
        max_message_bytes=1024,
        http_host=None,
        http_port=None,
        tls_cert_file=Path("cert.pem"),
        tls_key_file=Path("key.pem"),
        ice_servers=[],
        webrtc_negotiation_timeout_seconds=10,
        webrtc_disconnected_grace_seconds=10,
        grpc_host=None,
        grpc_port=0,
    )
    assert updated.aha_grpc.host == "127.0.0.2"
    assert updated.aha_grpc.port == 0


@pytest.mark.asyncio
async def test_runner_stops_http_when_grpc_exits(monkeypatch):
    from types import SimpleNamespace
    from audio_harness.serving import runner

    stopped = []

    class Host:
        def __init__(self, config):
            self.http_app = object()

            async def grpc_exit():
                pass

            self.grpc_server = SimpleNamespace(wait_for_termination=grpc_exit)

        async def start(self):
            pass

        async def stop(self):
            stopped.append("host")

    class Http:
        def __init__(self, config):
            self.should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0)
            stopped.append("http")

    monkeypatch.setattr(runner, "AudioHarnessHost", Host)
    monkeypatch.setattr(runner, "_ManagedUvicornServer", Http)
    with pytest.raises(RuntimeError, match="aha-grpc 服务意外停止"):
        await runner.run_server(AppConfig())
    assert stopped == ["http", "host"]
