from __future__ import annotations

import asyncio
from contextlib import aclosing
from types import SimpleNamespace

import httpx
import pytest

from aha.contracts.json_codec import encode_server_event
from audio_harness.configs.app import AppConfig
from audio_harness.configs.workflow import WorkflowRuntimeConfig
from audio_harness.database.session import upgrade_database
from audio_harness.engines.models import EngineApi
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.media.pcm import PcmChunk
from audio_harness.sessions.physical_vad.session import PhysicalVadSession
from audio_harness.serving.host import AudioHarnessHost
from audio_harness.serving.realtime.session import RealtimeTranscriptionSession
from audio_harness.workflows.compiler import AudioGraphValidationError, compile_audio_graph
from audio_harness.workflows.models import PhysicalVadConfig
from audio_harness.workflows.runtime import AudioInferenceRequest, AudioRuntimeConfig, AudioWorkflowRuntime
from audio_harness.workflows.service import ActiveAudioWorkflow
from tests.test_diarization import _Asr, _Vad
from tests.test_streaming_diarization import FakeEngine
from tests.test_streaming_diarization_workflow import assets, workflow


def vad_session(*, holdback=1, maximum=60):
    return PhysicalVadSession(
        PhysicalVadConfig(
            min_segment_seconds=0.5, max_segment_seconds=maximum,
            audio_buffer_seconds=120, speech_pre_roll_seconds=0,
            speech_chunk_seconds=1, speech_chunk_holdback_seconds=holdback,
        ), 4, _Vad(), speech_chunks_enabled=True,
    )


def chunks(values):
    return [value for port, value in values if port == "speech_chunks"]


def test_chunks_precede_stop_and_retain_one_source_and_exact_pcm():
    state = vad_session()
    pcm = bytes(range(24))
    state.append(PcmChunk(pcm, 4, 0))
    first = state.update(VadInferencer.Update(((0, None),), 8))
    early = chunks(first)
    assert [(c.start_sample, c.end_sample, c.completed) for c in early] == [(0, 4, False)]
    assert not any(port in {"segments", "speech_stopped"} for port, _ in first)
    # 停顿判定发生在 processed=12，实际语音结束在 6；保留的尾部不能输出静音。
    final = state.update(VadInferencer.Update(((None, 6),), 12))
    all_chunks = early + chunks(final)
    assert b"".join(c.audio for c in all_chunks) == pcm[:12]
    assert [(c.start_sample, c.end_sample) for c in all_chunks] == [(0, 4), (4, 6)]
    assert all_chunks[-1].completed
    assert len({c.physical_segment_id for c in all_chunks}) == 1
    assert {c.source_start_sample for c in all_chunks} == {0}
    assert next(value for port, value in final if port == "segments").audio == pcm[:12]


def test_exact_boundary_has_empty_completion_without_repeating_audio():
    state = vad_session(holdback=0)
    pcm = bytes(range(16))
    state.append(PcmChunk(pcm, 4, 0))
    early = chunks(state.update(VadInferencer.Update(((0, None),), 8)))
    final = chunks(state.update(VadInferencer.Update(((None, 8),), 8)))
    assert len(final) == 1 and final[0].completed and final[0].audio == b""
    assert final[0].start_sample == final[0].end_sample == 8
    assert b"".join(c.audio for c in early + final) == pcm


def test_eof_flushes_short_tail_even_below_min_segment_length():
    state = vad_session(holdback=0)
    pcm = bytes(range(10))
    state.append(PcmChunk(pcm, 4, 0))
    early = chunks(state.update(VadInferencer.Update(((0, None),), 5)))
    final = chunks(state.finish())
    assert b"".join(c.audio for c in early + final) == pcm
    assert final[-1].audio == pcm[8:] and final[-1].completed
    assert state.finish() == []


def test_late_boundary_cannot_silently_retract_committed_audio():
    state = vad_session(holdback=0)
    state.append(PcmChunk(bytes(24), 4, 0))
    state.update(VadInferencer.Update(((0, None),), 8))
    with pytest.raises(ValueError, match="speech_chunk_holdback_seconds"):
        state.update(VadInferencer.Update(((None, 6),), 12))


def test_max_length_splits_close_each_source_and_cover_audio_once():
    state = vad_session(holdback=0, maximum=2)
    pcm = bytes(range(40))
    state.append(PcmChunk(pcm, 4, 0))
    early = chunks(state.update(VadInferencer.Update(((0, None),), 20)))
    final = chunks(state.finish())
    values = early + final
    assert b"".join(c.audio for c in values) == pcm
    assert sum(c.completed for c in values) == 3
    assert len({c.physical_segment_id for c in values}) == 3


def chunk_workflow():
    document = workflow()
    vad = next(node for node in document.nodes if node.kind == "physical_vad")
    vad.config = PhysicalVadConfig(speech_pre_roll_seconds=0, speech_chunk_holdback_seconds=0)
    for edge in document.edges:
        if edge.target.node_id == "diarization" and edge.target.port == "segments":
            edge.source.port = "speech_chunks"
    return document


def test_speech_chunks_only_connect_to_streaming_consumers():
    document = chunk_workflow()
    compile_audio_graph(document, assets=assets())
    edge = next(edge for edge in document.edges if edge.source.kind == "node" and edge.source.port == "speech_chunks")
    edge.target.node_id = "asr"
    with pytest.raises(AudioGraphValidationError) as error:
        compile_audio_graph(document, assets=assets())
    assert any(d.code == "port_type_mismatch" for d in error.value.diagnostics)


def test_compiler_rejects_insufficient_speech_chunk_buffer_before_publish():
    document = chunk_workflow()
    vad = next(node for node in document.nodes if node.kind == "physical_vad")
    vad.config.speech_chunk_seconds = 121
    with pytest.raises(AudioGraphValidationError) as error:
        compile_audio_graph(document, assets=assets())
    assert any(d.code == "speech_chunk_buffer_too_small" for d in error.value.diagnostics)
    # 没有使用增量出口的旧工作流不受新增约束影响。
    for edge in document.edges:
        if edge.source.kind == "node" and edge.source.port == "speech_chunks":
            edge.source.port = "segments"
    compile_audio_graph(document, assets=assets())


class LiveVad(_Vad):
    async def stream_detect(self, audio_chunks, sample_rate, *, postprocessing=None):
        total = 0
        async for pcm in audio_chunks:
            first = total == 0
            total += len(pcm) // 2
            yield self.Update(((0, None),) if first else (), total)
        yield self.Update(((None, total),), total)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed, tail_samples, vad_events", [(False, 8000, True), (False, 1, True), (False, 0, False), (True, 8000, True), (True, 0, True)])
async def test_open_speaker_updates_do_not_fragment_asr(failed, tail_samples, vad_events):
    release = asyncio.Event()
    input_finished = False
    asr = _Asr()
    engine = FakeEngine(fail=failed)
    document = chunk_workflow()
    plan = compile_audio_graph(document, assets=assets())
    runtime = AudioWorkflowRuntime(
        plan=plan,
        inferencers={"vad": LiveVad(), "asr": asr, "diarization": engine},
        runtime_config=AudioRuntimeConfig(1, 1), asr_semaphore=asyncio.Semaphore(1),
    )
    pcm = bytes((1, 0)) * (32000 + tail_samples)

    async def audio():
        nonlocal input_finished
        yield PcmChunk(pcm[:32000], 16000, 0)
        await release.wait()
        yield PcmChunk(pcm[32000:64000], 16000, 16000)
        if tail_samples:
            yield PcmChunk(pcm[64000:], 16000, 32000)
        input_finished = True

    results = []
    sent = []

    async def send_event(event):
        sent.append(encode_server_event(event))

    realtime = RealtimeTranscriptionSession(
        send_event=send_event, session_service=object(),
        workflow_settings=SimpleNamespace(current=lambda: SimpleNamespace(config=WorkflowRuntimeConfig())),
        model="streaming-test", target=ActiveAudioWorkflow("streaming-test", 1, 1, document, plan),
        aha_version="v1",
    )
    request = AudioInferenceRequest(
        recognition=AsrInferencer.Options(16000, "zh-CN"),
        include_diarization=True, include_vad_events=vad_events,
    )
    async with aclosing(runtime.stream(audio(), request)) as stream:
        while not any(output.name == "diarization_local" for output in results):
            output = await asyncio.wait_for(anext(stream), 2)
            results.append(output)
            if vad_events:
                await realtime._handle_workflow_event(output.name, output.payload)
        assert not input_finished
        assert any(output.name == "diarization_local" for output in results)
        assert not any(output.name == "primary_transcript" for output in results)
        assert asr.calls == []
        assert not any(output.name == "speaker_transcript" for output in results)
        assert not any(output.name == "diarization_local" and output.payload.phase == "completed" for output in results)
        if vad_events:
            assert any(event["type"] == "x_aha.diarization.local.delta" for event in sent)
            assert not any(event["type"] == "x_aha.input_audio_transcription.speaker.completed" for event in sent)
            assert not any(event["type"] == "input_audio_buffer.speech_stopped" for event in sent)
        release.set()

        async def drain():
            return [output async for output in stream]

        results.extend(await asyncio.wait_for(drain(), 3))
    assert input_finished
    assert b"".join(asr.calls) == pcm
    assert asr.calls == [pcm]
    speaker = [output.payload for output in results if output.name == "speaker_transcript"]
    assert len({value.item_source_id for value in speaker}) == 1
    primary = [output.payload for output in results if output.name == "primary_transcript"]
    assert len(primary) == 1 and primary[0].is_final and primary[0].end_sample == len(pcm) // 2
    assert engine.closed
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("diarization-duplex-")]


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_seconds", [1, 0.32])
async def test_only_next_speaker_triggers_transcription_not_model_closure(chunk_seconds):
    close_speaker = asyncio.Event()
    release_audio = asyncio.Event()
    finish_audio = asyncio.Event()
    finished = False
    asr = _Asr()
    initial_samples = 32000 if chunk_seconds == 1 else 30720
    total_samples = initial_samples + 16000
    split = initial_samples * 2
    pcm = bytes((1, 0)) * total_samples

    class BoundaryEngine(FakeEngine):
        async def stream_diarize(self, audio, sample_rate, *, input_buffer_latency_ms):
            first = await anext(audio)
            assert len(first) == split
            yield self.Update((self.Boundary(1, 1, 0, None),), initial_samples)
            await close_speaker.wait()
            yield self.Update((self.Boundary(1, 1, None, initial_samples),), initial_samples)
            async for _ in audio:
                yield self.Update((self.Boundary(2, 2, initial_samples, None),), total_samples)
            yield self.Update((self.Boundary(2, 2, None, total_samples),), total_samples)

    async def audio():
        nonlocal finished
        yield PcmChunk(pcm[:split], 16000, 0)
        await release_audio.wait()
        yield PcmChunk(pcm[split:], 16000, initial_samples)
        await finish_audio.wait()
        finished = True

    document = chunk_workflow()
    next(n for n in document.nodes if n.kind == "physical_vad").config.speech_chunk_seconds = chunk_seconds
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(document, assets=assets()),
        inferencers={"vad": LiveVad(), "asr": asr, "diarization": BoundaryEngine()},
        runtime_config=AudioRuntimeConfig(1, 1), asr_semaphore=asyncio.Semaphore(1),
    )
    request = AudioInferenceRequest(recognition=AsrInferencer.Options(16000, "zh-CN"), include_diarization=True)
    values = []
    async with aclosing(runtime.stream(audio(), request)) as stream:
        while not any(o.name == "diarization_local" and o.payload.batch_end_sample == initial_samples for o in values):
            values.append(await asyncio.wait_for(anext(stream), 2))
        assert asr.calls == []
        previews = [o.payload for o in values if o.name == "diarization_local"]
        assert {p.phase for p in previews} == {"delta"}
        assert len({p.segments[0].segment_id for p in previews}) == 1
        close_speaker.set()
        # Closing the model segment alone must leave the ongoing speaker buffered.
        await asyncio.sleep(0.05)
        assert asr.calls == []
        release_audio.set()
        while not any(o.name == "speaker_transcript" for o in values):
            values.append(await asyncio.wait_for(anext(stream), 2))
        assert not finished
        assert asr.calls == [pcm[:split]]
        assert not any(o.name == "speech_stopped" for o in values)
        closed = next(o.payload for o in values if o.name == "diarization_local" and o.payload.phase == "completed")
        assert closed.segments[0].segment_id == previews[0].segments[0].segment_id
        finish_audio.set()
        async def drain():
            return [o async for o in stream]
        values.extend(await asyncio.wait_for(drain(), 3))
    assert asr.calls == [pcm[:split], pcm[split:]]
    assert len([o for o in values if o.name == "diarization_local" and o.payload.phase == "completed"]) == 2


@pytest.mark.asyncio
async def test_speech_chunk_graph_can_be_configured_validated_and_published_via_api(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'streaming.db'}"
    upgrade_database(database_url)
    host = AudioHarnessHost(AppConfig.model_validate({"database": {"url": database_url}}))
    factories = {EngineApi.UX_VAD: LiveVad, EngineApi.UX_SPEECH: _Asr, EngineApi.UX_SPEAKER_DIARIZATION_DUPLEX: FakeEngine}
    host.inferencer_pool._create_inferencer = lambda snapshot: factories[snapshot.api]()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.http_app), base_url="http://test") as client:
            catalog = (await client.get("/api/v1/audio-node-types")).json()
            physical = next(spec for spec in catalog if spec["kind"] == "physical_vad")
            assert {"name": "speech_chunks", "kind": "speech_audio_chunk", "accepted_kinds": []} in physical["outputs"]
            document = chunk_workflow()
            for node in document.nodes:
                asset = assets()[{"physical_vad": "vad-engine", "asr": "asr-engine", "streaming_speaker_diarization": "diarization-engine"}[node.kind]]
                response = await client.post("/api/v1/engines", json={
                    "slug": asset.id, "name": asset.name, "kind": asset.kind.value,
                    "api": asset.api.value, "protocol": "grpc", "target": "localhost:50051",
                })
                assert response.status_code == 201, response.text
                node.engine_id = response.json()["id"]
            created = await client.post("/api/v1/audio-workflows", json={"slug": "streaming-test", "name": "流式测试", "document": document.model_dump(mode="json")})
            assert created.status_code == 201, created.text
            base = f"/api/v1/audio-workflows/{created.json()['id']}"
            validation = await client.post(base + "/validate")
            assert validation.json()["valid"], validation.text
            published = await client.post(base + "/publish", json={"expected_revision": created.json()["draft_revision"]})
            assert published.status_code == 200, published.text
            active = await client.get(base + "/active")
            assert active.json() == published.json()
            assert any(edge["source"].get("port") == "speech_chunks" for edge in active.json()["document"]["edges"])
    finally:
        await host.stop()
