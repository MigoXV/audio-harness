from __future__ import annotations

from collections.abc import AsyncIterator

import numpy as np
import pytest

from audio_harness.engines.models import EngineApi
from audio_harness.inferencers.embedding.dummy import DummyClusteringInferencer
from audio_harness.sessions.contracts import DiarizationContext, SegmentCandidate
from audio_harness.sessions.speaker_clustering.session import SpeakerClusteringSession
from audio_harness.workflows.compiler import _api_satisfies
from audio_harness.workflows.models import FailurePolicy, SpeakerClusteringNodeConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("normalize", [True, False])
async def test_dummy_embedding_is_finite_unit_float32(normalize: bool) -> None:
    inferencer = DummyClusteringInferencer()
    await inferencer.ready()
    await inferencer.probe()
    kwargs = dict(algorithm="unused", expected_dimension=3, normalize=normalize)
    assert await inferencer.embed(b"\x01\x00", **kwargs) == (1.0, 0.0, 0.0)
    raw = await inferencer.embed_bytes(b"\x01\x00", **kwargs)
    assert len(raw) == 12
    np.testing.assert_array_equal(np.frombuffer(raw, dtype="<f4"), [1, 0, 0])
    await inferencer.close()
    await inferencer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("audio", "dimension"), [(b"", 2), (b"x", 2), (b"xx", 0), (b"xx", -1)])
async def test_dummy_embedding_rejects_invalid_input(audio: bytes, dimension: int) -> None:
    with pytest.raises(ValueError):
        await DummyClusteringInferencer().embed_bytes(
            audio, algorithm="unused", expected_dimension=dimension,
        )


@pytest.mark.asyncio
async def test_dummy_streaming_state_and_isolation() -> None:
    inferencer = DummyClusteringInferencer()
    first, second = inferencer.streaming_clustering(), inferencer.streaming_clustering()
    with pytest.raises(RuntimeError, match="尚未初始化"):
        await first.add((b"v",))
    with pytest.raises(ValueError):
        await first.start((), algorithm="unused", parameters={})
    assert await first.start((b"v",), algorithm="unused", parameters={}) == (0,)
    with pytest.raises(RuntimeError, match="已经初始化"):
        await first.start((b"v",), algorithm="unused", parameters={})
    with pytest.raises(ValueError):
        await first.add(())
    assert await first.add((b"v", b"w")) == (0, 0, 0)
    assert await second.start((b"v",), algorithm="unused", parameters={}) == (0,)
    await first.close()
    await first.close()
    with pytest.raises(RuntimeError, match="已经关闭"):
        await first.add((b"v",))
    with pytest.raises(RuntimeError, match="已经关闭"):
        await first.start((b"v",), algorithm="unused", parameters={})
    assert await second.add((b"w",)) == (0, 0)
    await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("postprocessing", [True, False])
async def test_dummy_clustering_keeps_zero_from_first_batch(postprocessing: bool) -> None:
    async def candidates() -> AsyncIterator[SegmentCandidate]:
        for index in range(3):
            yield SegmentCandidate(
                audio=b"\x01\x00" * 2,
                start_sample=index * 2,
                end_sample=index * 2 + 2,
                diarization=DiarizationContext(
                    item_source_id="source", source_start_sample=0,
                    source_end_sample=6, source_completed=index == 2,
                    segment_id=f"segment-{index}",
                    batch_id="first" if index == 0 else "second",
                    speaker=index, local_speaker_id=f"local-{index}",
                    min_asr_segment_seconds=0, batch_completed=index in (0, 2),
                ),
            )

    config = SpeakerClusteringNodeConfig(expected_dimension=2)
    if not postprocessing:
        config = config.model_copy(update={"postprocessing": None})
    session = SpeakerClusteringSession(
        config, 1, DummyClusteringInferencer(),
        failure_policy=FailurePolicy.FAIL_PIPELINE,
    )
    results = [(port, result) async for port, result in session.run(candidates())]
    assert [port for port, _ in results] == ["results", "results"]
    assert [result.phase for _, result in results] == ["completed", "completed"]
    assert [result.revision for _, result in results] == [1, 2]
    assert [result.replaces_revision for _, result in results] == [None, 1]
    assert [[a.speaker_id for a in result.assignments] for _, result in results] == [[0], [0, 0, 0]]
    assert [(a.segment_id, a.local_speaker_id) for a in results[-1][1].assignments] == [
        (f"segment-{i}", f"local-{i}") for i in range(3)
    ]


@pytest.mark.parametrize("kind", ["speaker_clustering", "audio_embedding"])
def test_dummy_api_is_only_compatible_with_clustering(kind: str) -> None:
    assert _api_satisfies(
        EngineApi.ENGINE_FEATURE, EngineApi.DUMMY_CLUSTERING, node_kind=kind,
    ) is (kind == "speaker_clustering")


@pytest.mark.asyncio
async def test_dummy_engine_compiles_restores_and_runs_workflow() -> None:
    import asyncio
    from dataclasses import replace

    from audio_harness.engines.models import EngineKind, EngineProtocol
    from audio_harness.inferencers.asr.base import AsrInferencer
    from audio_harness.workflows.compiler import (
        compile_audio_graph, compiled_audio_graph_document, restore_audio_graph,
    )
    from audio_harness.workflows.runtime import (
        AudioInferenceRequest, AudioRuntimeConfig, AudioWorkflowRuntime,
    )
    from tests.test_diarization import _asset, _cluster_workflow, _Asr, _Diarization, _Vad, _audio

    assets = {
        "vad-engine": _asset("vad-engine", EngineKind.VAD, EngineApi.UX_VAD),
        "asr-engine": _asset("asr-engine", EngineKind.ASR, EngineApi.UX_SPEECH),
        "diarization-engine": _asset("diarization-engine", EngineKind.DIARIZATION, EngineApi.UX_SPEAKER_DIARIZATION),
        "clustering-engine": replace(
            _asset("clustering-engine", EngineKind.EMBEDDING, EngineApi.DUMMY_CLUSTERING),
            protocol=EngineProtocol.DUMMY, target="dummy://clustering",
        ),
    }
    definition = _cluster_workflow()
    plan = compile_audio_graph(definition, assets=assets)
    restored = restore_audio_graph(definition, compiled_audio_graph_document(plan))
    assert restored.nodes["clustering"].engine_snapshot.api is EngineApi.DUMMY_CLUSTERING
    runtime = AudioWorkflowRuntime(
        plan=restored,
        inferencers={"vad": _Vad(), "asr": _Asr(), "diarization": _Diarization(), "clustering": DummyClusteringInferencer()},
        runtime_config=AudioRuntimeConfig(4, 32), asr_semaphore=asyncio.Semaphore(1),
    )
    outputs = [output async for output in runtime.stream(
        _audio(), AudioInferenceRequest(recognition=AsrInferencer.Options(1, "zh-CN"), include_diarization=True),
    )]
    result = next(output.payload for output in outputs if output.name == "diarization_cluster")
    assert result.phase == "completed"
    assert [a.speaker_id for a in result.assignments] == [0, 0]
