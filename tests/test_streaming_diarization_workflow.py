from __future__ import annotations

import asyncio

import pytest
import httpx

from aha.contracts.protocol import AHA_REALTIME_SERVER_EVENT_ADAPTER
from audio_harness.engines.catalog import EngineCatalog
from audio_harness.configs.app import AppConfig
from audio_harness.database.session import upgrade_database
from audio_harness.engines.models import EngineApi, EngineKind
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.media.pcm import PcmChunk
from audio_harness.serving.realtime.session import RealtimeTranscriptionSession
from audio_harness.serving.host import AudioHarnessHost
from audio_harness.workflows.compiler import AudioGraphValidationError, compile_audio_graph
from audio_harness.workflows.models import (
    AudioEdge, GraphInputRef, NamedOutputBinding, NodeInputRef, NodeOutputRef,
    StreamingSpeakerDiarizationNode,
)
from audio_harness.workflows.runtime import AudioInferenceRequest, AudioRuntimeConfig, AudioWorkflowRuntime
from tests.test_diarization import _Asr, _Vad, _asset, _speaker_workflow
from tests.test_streaming_diarization import FakeEngine, iterate


def workflow():
    document = _speaker_workflow()
    return document.model_copy(update={
        "nodes": [StreamingSpeakerDiarizationNode(id=node.id, engine_id=node.engine_id)
                  if node.kind == "speaker_diarization" else node for node in document.nodes],
        "edges": [*document.edges, AudioEdge(
            id="audio-duplex", source=GraphInputRef(),
            target=NodeInputRef(node_id="diarization", port="audio"),
        )],
        "outputs": [*document.outputs, NamedOutputBinding(
            name="diarization_cluster", source=NodeOutputRef(node_id="diarization", port="cluster_results"),
        )],
    })


def assets():
    return {
        "vad-engine": _asset("vad-engine", EngineKind.VAD, EngineApi.UX_VAD),
        "asr-engine": _asset("asr-engine", EngineKind.ASR, EngineApi.UX_SPEECH),
        "diarization-engine": _asset("diarization-engine", EngineKind.DIARIZATION, EngineApi.UX_SPEAKER_DIARIZATION_DUPLEX),
    }


@pytest.mark.asyncio
async def test_duplex_engine_registration_and_node_catalog(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'duplex.db'}"
    upgrade_database(url)
    host = AudioHarnessHost(AppConfig.model_validate({
        "database": {"url": url}, "voices": {"storage_dir": tmp_path / "voices"},
    }))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.http_app), base_url="http://test") as client:
            response = await client.post("/api/v1/engines", json={
                "slug": "sortformer", "name": "Sortformer", "kind": "diarization",
                "api": "ux_speaker_diarization_duplex", "protocol": "grpc",
                "target": "localhost:50051", "connect_timeout_seconds": 2,
                "request_timeout_seconds": None,
            })
            assert response.status_code == 201, response.text
            engine = response.json()
            fetched = await client.get(f"/api/v1/engines/{engine['id']}")
            assert fetched.json()["api"] == "ux_speaker_diarization_duplex"
            assert fetched.json()["request_timeout_seconds"] is None
            types = await client.get("/api/v1/audio-node-types")
            assert types.status_code == 200
            node = next(value for value in types.json() if value["kind"] == "streaming_speaker_diarization")
            assert {port["name"] for port in node["inputs"]} == {"audio", "segments"}
            assert node["required_api"] == engine["api"]
    finally:
        await host.stop()


def test_catalog_accepts_duplex_and_compiler_requires_both_inputs():
    engines = assets()
    EngineCatalog._validate_asset_shape(engines["diarization-engine"])
    document = workflow()
    compile_audio_graph(document, assets=engines)
    for port in ("audio", "segments"):
        invalid = document.model_copy(update={"edges": [
            edge for edge in document.edges
            if not (edge.target.node_id == "diarization" and edge.target.port == port)
        ]})
        with pytest.raises(AudioGraphValidationError):
            compile_audio_graph(invalid, assets=engines)
    engines["diarization-engine"] = _asset("diarization-engine", EngineKind.DIARIZATION, EngineApi.UX_SPEAKER_DIARIZATION)
    with pytest.raises(AudioGraphValidationError):
        compile_audio_graph(document, assets=engines)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_duplex_graph_with_tiny_queues_preserves_asr_and_public_events(failed):
    asr = _Asr()
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(workflow(), assets=assets()),
        inferencers={"vad": _Vad(), "asr": asr, "diarization": FakeEngine(fail=failed)},
        runtime_config=AudioRuntimeConfig(1, 1), asr_semaphore=asyncio.Semaphore(1),
    )
    pcm = bytes((1, 0)) * 32000

    async def run():
        return [output async for output in runtime.stream(
            iterate([PcmChunk(pcm[offset:offset + 640], 16000, offset // 2) for offset in range(0, len(pcm), 640)]),
            AudioInferenceRequest(recognition=AsrInferencer.Options(16000, "zh-CN"), include_diarization=True),
        )]

    outputs = await asyncio.wait_for(run(), 5)
    assert b"".join(asr.calls) == pcm
    assert len([output for output in outputs if output.name == "primary_transcript"]) == 1
    clusters = [output.payload for output in outputs if output.name == "diarization_cluster"]
    assert clusters[-1].phase == ("failed" if failed else "completed")
    # Existing Realtime serializers and the shared AHA contract accept these
    # outputs without introducing a separate Sortformer event family.
    sent = []

    async def send(event):
        payload = event.model_dump(mode="json", exclude_none=True)
        AHA_REALTIME_SERVER_EVENT_ADAPTER.validate_python(payload)
        sent.append(payload)

    realtime = object.__new__(RealtimeTranscriptionSession)
    realtime._aha_version = "v1"
    realtime._send_event = send
    realtime._cluster_output_enabled = True
    realtime._discard_workflow_outputs = False
    realtime._workflow_failure = None
    realtime._cluster_latest_result = None
    realtime._cluster_last_completed = None
    for output in outputs:
        if output.name in {"diarization_local", "diarization_cluster"}:
            await realtime._handle_workflow_event(output.name, output.payload)
    await realtime._emit_final_cluster_snapshot(32000)
    assert sent[-1]["final"] is True and sent[-1]["processed_audio_ms"] == 2000
    assert sent[-1]["type"] == f"x_aha.diarization.cluster.{'failed' if failed else 'completed'}"
    if not failed:
        speaker = next(output.payload for output in outputs if output.name == "speaker_transcript")
        assert speaker.segment_id == clusters[-1].assignments[0].segment_id
        assert sent[-1]["assignments"][0]["speaker_id"] == 1
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("diarization-duplex-")]


@pytest.mark.asyncio
async def test_concurrent_sessions_do_not_share_identity_or_revision():
    shared = FakeEngine()
    async def run():
        runtime = AudioWorkflowRuntime(
            plan=compile_audio_graph(workflow(), assets=assets()),
            inferencers={"vad": _Vad(), "asr": _Asr(), "diarization": shared},
            runtime_config=AudioRuntimeConfig(1, 1), asr_semaphore=asyncio.Semaphore(1),
        )
        return [output.payload async for output in runtime.stream(
            iterate([PcmChunk(bytes(32000), 16000, 0)]),
            AudioInferenceRequest(recognition=AsrInferencer.Options(16000, "zh-CN"), include_diarization=True),
        ) if output.name == "diarization_cluster"]
    first, second = await asyncio.wait_for(asyncio.gather(run(), run()), 5)
    assert first[0].revision == second[0].revision == 1
    assert first[0].assignments[0].segment_id != second[0].assignments[0].segment_id
