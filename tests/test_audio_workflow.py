from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import numpy as np
import pytest
from pydantic import ValidationError

from audio_harness.engines.models import (
    EngineApi,
    EngineAssetData,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.errors import CapacityExceededError
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.embedding.base import EngineFeatureInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.media.pcm import PcmChunk
from audio_harness.sessions.contracts import SegmentCandidate as _SegmentCandidate
from audio_harness.sessions.physical_vad.session import (
    PhysicalVadSession,
    VadProgressLimiter,
)
from audio_harness.sessions.semantic_vad.session import SemanticVadSession
from audio_harness.workflows.models import (
    AsrNode,
    AsrNodeConfig,
    AudioEdge,
    AudioWorkflowDocument,
    FailurePolicy,
    GraphInputRef,
    NamedOutputBinding,
    NodeInputRef,
    NodeOutputRef,
    PhysicalVadConfig,
    PhysicalVadNode,
    SemanticVadNode,
    AudioEmbeddingNode,
    AudioEmbeddingNodeConfig,
    HdbscanClusteringParameters,
    SpeakerClusteringNodeConfig,
    SpeakerFilterNode,
    SpeakerFilterNodeConfig,
    VectorSearchNode,
    VectorSearchNodeConfig,
)
from audio_harness.workflows.compiler import (
    AudioGraphValidationError,
    compile_audio_graph,
    speech_started_physical_vad,
)
from audio_harness.workflows.runtime import (
    AudioInferenceRequest,
    AudioRuntimeConfig,
    AudioWorkflowRuntime,
)
from audio_harness.workflows.events import (
    SpeakerFilterResult,
    SpeechStarted,
    SpeechStopped,
    VadProgress,
    VectorSearchResult,
)
from audio_harness.vectors.models import SpeakerMatch, VectorObservation


def _asset(asset_id: str, kind: EngineKind, api: EngineApi) -> EngineAssetData:
    now = datetime.now(timezone.utc)
    return EngineAssetData(
        id=asset_id,
        slug=asset_id,
        name=asset_id,
        kind=kind,
        api=api,
        protocol=EngineProtocol.GRPC,
        target="127.0.0.1:50051",
        connect_timeout_seconds=1,
        request_timeout_seconds=None,
        enabled=True,
        status=EngineStatus.CONFIRMED_RUNNING,
        image=None,
        description=None,
        status_basis=None,
        status_observed_at=None,
        source=None,
        details={},
        revision=1,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def assets() -> dict[str, EngineAssetData]:
    return {
        "vad-engine": _asset("vad-engine", EngineKind.VAD, EngineApi.UX_VAD),
        "asr-engine": _asset("asr-engine", EngineKind.ASR, EngineApi.UX_SPEECH),
    }


def _definition(*, semantic: bool = True) -> AudioWorkflowDocument:
    nodes = [
        PhysicalVadNode(
            id="physical",
            engine_id="vad-engine",
            config=PhysicalVadConfig(
                min_segment_seconds=0,
                max_segment_seconds=60,
                audio_buffer_seconds=61,
                speech_pre_roll_seconds=0,
            ),
        ),
        AsrNode(id="asr", engine_id="asr-engine"),
    ]
    edges = [
        AudioEdge(
            id="input-physical",
            source=GraphInputRef(),
            target=NodeInputRef(node_id="physical", port="audio"),
        )
    ]
    segment_source = "physical"
    stopped_source = "physical"
    if semantic:
        nodes.insert(
            1,
            SemanticVadNode(
                id="semantic",
                engine_id="vad-engine",
                failure_policy=FailurePolicy.FAIL_OPEN,
            ),
        )
        edges.append(
            AudioEdge(
                id="physical-semantic",
                source=NodeOutputRef(node_id="physical", port="segments"),
                target=NodeInputRef(node_id="semantic", port="segments"),
            )
        )
        segment_source = "semantic"
        stopped_source = "semantic"
    edges.append(
        AudioEdge(
            id="segments-asr",
            source=NodeOutputRef(node_id=segment_source, port="segments"),
            target=NodeInputRef(node_id="asr", port="segments"),
        )
    )
    return AudioWorkflowDocument(
        nodes=nodes,
        edges=edges,
        outputs=[
            NamedOutputBinding(
                name="primary_transcript",
                source=NodeOutputRef(node_id="asr", port="transcripts"),
            ),
            NamedOutputBinding(
                name="speech_started",
                source=NodeOutputRef(node_id="physical", port="speech_started"),
            ),
            NamedOutputBinding(
                name="speech_stopped",
                source=NodeOutputRef(node_id=stopped_source, port="speech_stopped"),
            ),
        ],
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {"min_speech_frame": 0},
        {"min_silence_frame": 1.5},
        {"smooth_window_size": True},
        {"speech_threshold": -0.1},
        {"speech_threshold": float("inf")},
        {"unknown": 1},
    ],
)
def test_physical_vad_rejects_invalid_postprocessing(parameters: dict) -> None:
    with pytest.raises(ValidationError):
        PhysicalVadConfig.model_validate(
            {
                "postprocessing": {
                    "processor": "firered.stream_vad",
                    "version": 1,
                    "parameters": parameters,
                }
            }
        )


@pytest.mark.parametrize("value", [-0.1, float("inf"), float("nan"), True])
def test_physical_vad_rejects_invalid_progress_interval(value: object) -> None:
    with pytest.raises(ValidationError):
        PhysicalVadConfig(progress_interval_seconds=value)


def test_vad_progress_limiter_coalesces_and_normalizes_updates() -> None:
    limiter = VadProgressLimiter(interval_seconds=0.2, sample_rate=1_000)

    first = limiter.update(100, available_samples=90, now=0)
    throttled = limiter.update(150, available_samples=150, now=0.1)
    second = limiter.update(120, available_samples=200, now=0.2)
    final = limiter.finish(187)

    assert first == VadProgress(processed_samples=90, sample_rate=1_000)
    assert throttled is None
    assert second == VadProgress(processed_samples=150, sample_rate=1_000)
    assert final == VadProgress(
        processed_samples=187,
        sample_rate=1_000,
        final=True,
    )


def test_physical_vad_pairs_boundaries_across_max_length_segments() -> None:
    state = PhysicalVadSession(
        PhysicalVadConfig(
            min_segment_seconds=0,
            max_segment_seconds=1,
            audio_buffer_seconds=3,
            speech_pre_roll_seconds=0,
        ),
        sample_rate=4,
        inferencer=_Vad([]),
    )
    state.append(
        PcmChunk(
            pcm_s16le=bytes(16),
            sample_rate=4,
            start_sample=0,
        )
    )

    first = state.update(
        VadInferencer.Update(
            boundaries=((0, None),),
            processed_samples=4,
        )
    )
    second = state.update(
        VadInferencer.Update(
            boundaries=((None, 8),),
            processed_samples=8,
        )
    )

    assert [(port, payload.start_sample) for port, payload in first] == [
        ("speech_started", 0),
    ]
    assert [(port, payload.start_sample) for port, payload in second] == [
        ("segments", 0),
        ("speech_stopped", 0),
        ("speech_started", 4),
        ("segments", 4),
        ("speech_stopped", 4),
    ]
    assert second[-1][1].end_sample == 8
    started = first[0][1]
    first_stopped = second[1][1]
    second_started = second[2][1]
    second_stopped = second[-1][1]
    assert isinstance(started, SpeechStarted)
    assert isinstance(first_stopped, SpeechStopped)
    assert isinstance(second_started, SpeechStarted)
    assert isinstance(second_stopped, SpeechStopped)
    assert started.physical_segment_id is not None
    assert first_stopped.physical_segment_id == started.physical_segment_id
    assert second_started.physical_segment_id != started.physical_segment_id
    assert (
        second_stopped.physical_segment_id
        == second_started.physical_segment_id
    )


def test_physical_vad_tail_drain_keeps_segment_id() -> None:
    state = PhysicalVadSession(
        PhysicalVadConfig(
            min_segment_seconds=0,
            max_segment_seconds=60,
            audio_buffer_seconds=61,
            speech_pre_roll_seconds=0,
        ),
        sample_rate=4,
        inferencer=_Vad([]),
    )
    state.append(
        PcmChunk(
            pcm_s16le=bytes(16),
            sample_rate=4,
            start_sample=0,
        )
    )
    started = state.update(
        VadInferencer.Update(
            boundaries=((0, None),),
            processed_samples=4,
        )
    )[0][1]

    final = state.finish()

    segment = final[0][1]
    stopped = final[-1][1]
    assert isinstance(started, SpeechStarted)
    assert isinstance(segment, _SegmentCandidate)
    assert isinstance(stopped, SpeechStopped)
    assert segment.physical_segment_id == started.physical_segment_id
    assert segment.physical_segment_ids == (started.physical_segment_id,)
    assert segment.physical_segment_completed is True
    assert stopped.physical_segment_id == started.physical_segment_id
    assert stopped.physical_segment_ids == (started.physical_segment_id,)


@pytest.mark.asyncio
async def test_semantic_vad_preserves_gaps_in_the_session_timeline() -> None:
    inferencer = _Vad([], turns=[False, True])
    state = SemanticVadSession(
        config=SemanticVadNode(id="semantic", engine_id="vad-engine").config,
        sample_rate=1,
        inferencer=inferencer,
    )

    first = await state.accept(
        _SegmentCandidate(
            bytes(4),
            start_sample=10,
            end_sample=12,
            physical_segment_ids=("pseg_a",),
        ),
        failure_policy=FailurePolicy.FAIL_OPEN,
    )
    second = await state.accept(
        _SegmentCandidate(
            bytes(4),
            start_sample=20,
            end_sample=22,
            physical_segment_completed=True,
            physical_segment_ids=("pseg_b",),
        ),
        failure_policy=FailurePolicy.FAIL_OPEN,
    )

    segment = next(payload for port, payload in second if port == "segments")
    stopped = next(
        payload for port, payload in second if port == "speech_stopped"
    )
    assert [port for port, _ in first] == ["turn_progress"]
    assert first[0][1].physical_segment_ids == ("pseg_a",)
    assert segment.audio == bytes(8)
    assert (segment.start_sample, segment.end_sample) == (10, 22)
    assert [
        (span.input_start_sample, span.input_end_sample, span.session_start_sample)
        for span in segment.timeline_spans
    ] == [(0, 2, 10), (2, 4, 20)]
    assert (stopped.start_sample, stopped.end_sample) == (10, 22)
    assert stopped.awaiting_physical_stop is True
    assert segment.physical_segment_ids == ("pseg_a", "pseg_b")
    assert stopped.physical_segment_ids == ("pseg_a", "pseg_b")


def test_speaker_clustering_uses_safe_hdbscan_defaults() -> None:
    config = SpeakerClusteringNodeConfig()

    assert config.clustering_algorithm == "hdbscan"
    assert config.parameters == HdbscanClusteringParameters(
        min_cluster_size=2,
        min_samples=2,
        cluster_selection_epsilon=0,
        allow_single_cluster=False,
    )


@pytest.mark.parametrize(
    "config",
    [
        {"clustering_algorithm": "dbscan"},
        {"parameters": {"min_cluster_size": 1}},
        {"parameters": {"min_samples": 0}},
        {"parameters": {"min_samples": True}},
        {"parameters": {"cluster_selection_epsilon": -0.01}},
        {"parameters": {"cluster_selection_epsilon": float("inf")}},
        {"parameters": {"cluster_selection_epsilon": "0.1"}},
        {"parameters": {"allow_single_cluster": "false"}},
        {"parameters": {"unknown": 1}},
    ],
)
def test_speaker_clustering_rejects_unsupported_hdbscan_config(
    config: dict,
) -> None:
    with pytest.raises(ValidationError):
        SpeakerClusteringNodeConfig.model_validate(config)


def test_compiler_builds_typed_plan(assets: dict[str, EngineAssetData]) -> None:
    plan = compile_audio_graph(_definition(), assets=assets)

    assert plan.topological_order == ("physical", "semantic", "asr")
    assert [snapshot.id for snapshot in plan.engine_snapshots] == [
        "asr-engine",
        "vad-engine",
    ]


def test_compiler_rejects_cycle(assets: dict[str, EngineAssetData]) -> None:
    definition = _definition()
    raw = definition.model_dump(mode="json")
    raw["edges"].append(
        {
            "id": "cycle",
            "source": {"kind": "node", "node_id": "semantic", "port": "segments"},
            "target": {"node_id": "semantic", "port": "segments"},
        }
    )

    with pytest.raises(AudioGraphValidationError) as captured:
        compile_audio_graph(AudioWorkflowDocument.model_validate(raw), assets=assets)

    assert {item.code for item in captured.value.diagnostics} >= {
        "implicit_fan_in",
    }


def test_compiler_rejects_wrong_output_type(assets: dict[str, EngineAssetData]) -> None:
    raw = _definition().model_dump(mode="json")
    primary = next(item for item in raw["outputs"] if item["name"] == "primary_transcript")
    primary["source"] = {
        "kind": "node",
        "node_id": "physical",
        "port": "speech_started",
    }

    with pytest.raises(AudioGraphValidationError) as captured:
        compile_audio_graph(AudioWorkflowDocument.model_validate(raw), assets=assets)

    assert any(item.code == "output_type_mismatch" for item in captured.value.diagnostics)


def test_runtime_rejects_wrong_node_capability_immediately(
    assets: dict[str, EngineAssetData],
) -> None:
    with pytest.raises(TypeError, match="physical.*VadInferencer"):
        AudioWorkflowRuntime(
            plan=compile_audio_graph(_definition(semantic=False), assets=assets),
            inferencers={"physical": _Asr(), "asr": _Asr()},
            runtime_config=AudioRuntimeConfig(1, 1),
            asr_semaphore=asyncio.Semaphore(1),
        )


def test_runtime_allows_nodes_to_share_one_stateless_inferencer(
    assets: dict[str, EngineAssetData],
) -> None:
    shared_vad = _Vad([])
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(_definition(), assets=assets),
        inferencers={
            "physical": shared_vad,
            "semantic": shared_vad,
            "asr": _Asr(),
        },
        runtime_config=AudioRuntimeConfig(1, 1),
        asr_semaphore=asyncio.Semaphore(1),
    )

    assert runtime._inferencers["physical"] is shared_vad
    assert runtime._inferencers["semantic"] is shared_vad


class _Vad(VadInferencer):
    def __init__(
        self,
        updates: list[VadInferencer.Update],
        turns: list[bool] | None = None,
    ) -> None:
        self.updates = updates
        self.turns = list(turns or [])
        self.postprocessing: VadInferencer.Postprocessing | None = None
        self.stream_calls = 0

    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
        *,
        postprocessing: VadInferencer.Postprocessing | None = None,
    ) -> AsyncIterator[VadInferencer.Update]:
        self.stream_calls += 1
        self.postprocessing = postprocessing
        async for _ in audio_chunks:
            pass
        for update in self.updates:
            yield update

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        return self.turns.pop(0)

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass


class _Asr(AsrInferencer):
    def __init__(self, transcript: str = "结果") -> None:
        self.calls: list[tuple[bytes, int, int, bool]] = []
        self.transcript = transcript

    async def transcribe(
        self,
        audio: bytes,
        *,
        start_sample: int,
        end_sample: int,
        turn_completed: bool,
        options: AsrInferencer.Options,
    ) -> AsyncIterator[AsrInferencer.Result]:
        self.calls.append((audio, start_sample, end_sample, turn_completed))
        yield AsrInferencer.Result(
            transcript=self.transcript,
            is_final=True,
            start_sample=start_sample,
            end_sample=end_sample,
            sample_rate=options.sample_rate,
            turn_completed=turn_completed,
            language_code=options.language_code,
            metadata={"source": "fake"},
        )

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass


class _Embedding(EngineFeatureInferencer):
    def __init__(self) -> None:
        pass

    async def embed(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> tuple[float, ...]:
        assert pcm_s16le
        assert algorithm == "eres2netv2"
        assert expected_dimension == 2
        assert normalize is True
        return (1.0, 0.0)

    async def embed_bytes(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> bytes:
        values = await self.embed(
            pcm_s16le,
            algorithm=algorithm,
            expected_dimension=expected_dimension,
            normalize=normalize,
        )
        return np.asarray(values, dtype="<f4").tobytes()

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass

    def streaming_clustering(self):
        raise AssertionError("audio embedding 节点不应启动聚类流")


class _Vectors:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure

    async def search_embedding(
        self,
        collection: str,
        vector: tuple[float, ...],
        *,
        vector_name: str,
        top_k: int,
        score_threshold: float | None = None,
        filter_json: dict | None = None,
    ) -> tuple[SpeakerMatch, ...]:
        if self.failure is not None:
            raise self.failure
        assert (collection, vector_name, vector, top_k) == (
            "voiceprints", "eres2netv2", (1.0, 0.0), 10,
        )
        assert score_threshold is None
        assert filter_json == {}
        return (SpeakerMatch(
            "speaker-1",
            0.95,
            VectorObservation("point-1", {"speaker_id": "speaker-1"}),
        ),)


class _FailingVad(_Vad):
    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
        *,
        postprocessing: VadInferencer.Postprocessing | None = None,
    ) -> AsyncIterator[VadInferencer.Update]:
        async for _ in audio_chunks:
            pass
        raise RuntimeError("preview vad failed")
        yield  # pragma: no cover


class _PreviewCapacityLimiter:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    @asynccontextmanager
    async def try_slot(self):
        raise CapacityExceededError("full")
        yield  # pragma: no cover


async def _chunks() -> AsyncIterator[PcmChunk]:
    yield PcmChunk(pcm_s16le=bytes(16), sample_rate=4, start_sample=0)


def _vector_definition() -> AudioWorkflowDocument:
    definition = _definition()
    definition.nodes.extend([
        AudioEmbeddingNode(
            id="embedding",
            engine_id="embedding-engine",
            config=AudioEmbeddingNodeConfig(
                algorithm="eres2netv2",
                expected_dimension=2,
                normalize=True,
            ),
        ),
        VectorSearchNode(
            id="vector-search",
            config=VectorSearchNodeConfig(
                collection_name="voiceprints",
                vector_name="eres2netv2",
            ),
        ),
    ])
    definition.edges.extend([
        AudioEdge(
            id="semantic-embedding",
            source=NodeOutputRef(node_id="semantic", port="segments"),
            target=NodeInputRef(node_id="embedding", port="segments"),
        ),
        AudioEdge(
            id="embedding-vector-search",
            source=NodeOutputRef(node_id="embedding", port="embeddings"),
            target=NodeInputRef(node_id="vector-search", port="embeddings"),
        ),
    ])
    definition.outputs.append(NamedOutputBinding(
        name="vector_search",
        source=NodeOutputRef(node_id="vector-search", port="results"),
    ))
    return definition


def _speaker_filter_definition(*, allowed: str = "speaker-1") -> AudioWorkflowDocument:
    definition = _vector_definition()
    definition.nodes.append(
        SpeakerFilterNode(
            id="speaker-filter",
            config=SpeakerFilterNodeConfig(
                allowed_speaker_ids=[allowed],
                min_score=0.75,
            ),
        )
    )
    definition.edges = [
        edge for edge in definition.edges if edge.id != "segments-asr"
    ]
    definition.edges.extend(
        [
            AudioEdge(
                id="semantic-filter",
                source=NodeOutputRef(node_id="semantic", port="segments"),
                target=NodeInputRef(node_id="speaker-filter", port="segments"),
            ),
            AudioEdge(
                id="search-filter",
                source=NodeOutputRef(node_id="vector-search", port="results"),
                target=NodeInputRef(node_id="speaker-filter", port="results"),
            ),
            AudioEdge(
                id="filter-asr",
                source=NodeOutputRef(node_id="speaker-filter", port="segments"),
                target=NodeInputRef(node_id="asr", port="segments"),
            ),
        ]
    )
    definition.outputs = [
        output
        for output in definition.outputs
        if output.name not in {"speech_started", "speech_stopped"}
    ]
    definition.outputs.extend(
        [
            NamedOutputBinding(
                name="speech_started",
                source=NodeOutputRef(
                    node_id="speaker-filter",
                    port="speech_started",
                ),
            ),
            NamedOutputBinding(
                name="speech_stopped",
                source=NodeOutputRef(
                    node_id="speaker-filter",
                    port="speech_stopped",
                ),
            ),
            NamedOutputBinding(
                name="speaker_filter",
                source=NodeOutputRef(
                    node_id="speaker-filter",
                    port="decisions",
                ),
            ),
        ]
    )
    return definition


def test_speaker_filter_speech_started_resolves_upstream_physical_vad(
    assets: dict[str, EngineAssetData],
) -> None:
    definition = _speaker_filter_definition()
    physical_engine_id = next(
        node.engine_id
        for node in definition.nodes
        if isinstance(node, PhysicalVadNode)
    )
    asr_engine_id = next(
        node.engine_id
        for node in definition.nodes
        if isinstance(node, AsrNode)
    )
    definition.nodes.extend(
        [
            PhysicalVadNode(
                id="other-physical",
                engine_id=physical_engine_id,
            ),
            AsrNode(id="other-asr", engine_id=asr_engine_id),
        ]
    )
    definition.edges.extend(
        [
            AudioEdge(
                id="audio-other-physical",
                source=GraphInputRef(),
                target=NodeInputRef(
                    node_id="other-physical",
                    port="audio",
                ),
            ),
            AudioEdge(
                id="other-physical-asr",
                source=NodeOutputRef(
                    node_id="other-physical",
                    port="segments",
                ),
                target=NodeInputRef(node_id="other-asr", port="segments"),
            ),
        ]
    )
    definition.outputs.append(
        NamedOutputBinding(
            name="interim_transcript",
            source=NodeOutputRef(
                node_id="other-asr",
                port="transcripts",
            ),
        )
    )
    plan = compile_audio_graph(
        definition,
        assets={
            **assets,
            "embedding-engine": _asset(
                "embedding-engine",
                EngineKind.EMBEDDING,
                EngineApi.ENGINE_FEATURE,
            ),
        },
    )

    physical_vad = speech_started_physical_vad(plan)

    assert physical_vad is not None
    assert physical_vad[0] == "physical"
    assert physical_vad[1].config == plan.nodes["physical"].definition.config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed", "search_failure", "expected_status", "asr_calls"),
    [
        ("speaker-1", None, "matched", 1),
        ("speaker-2", None, "rejected", 0),
        ("speaker-1", RuntimeError("qdrant unavailable"), "failed", 0),
    ],
)
async def test_speaker_filter_is_fail_closed_and_only_passes_allowed_speaker(
    assets: dict[str, EngineAssetData],
    allowed: str,
    search_failure: Exception | None,
    expected_status: str,
    asr_calls: int,
) -> None:
    update = VadInferencer.Update(boundaries=((0, 8),), processed_samples=8)
    plan_assets = {
        **assets,
        "embedding-engine": _asset(
            "embedding-engine",
            EngineKind.EMBEDDING,
            EngineApi.ENGINE_FEATURE,
        ),
    }
    asr = _Asr()
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(
            _speaker_filter_definition(allowed=allowed),
            assets=plan_assets,
        ),
        inferencers={
            "physical": _Vad([update]),
            "semantic": _Vad([], turns=[True]),
            "asr": asr,
            "embedding": _Embedding(),
        },
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=asyncio.Semaphore(1),
        vectors=_Vectors(failure=search_failure),  # type: ignore[arg-type]
    )

    outputs = [
        output
        async for output in runtime.stream(
            _chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(
                    sample_rate=4,
                    language_code="zh-CN",
                ),
                include_vad_events=True,
                include_vad_progress=True,
                include_speaker_filter=True,
            ),
        )
    ]

    decision = next(
        output.payload for output in outputs if output.name == "speaker_filter"
    )
    assert isinstance(decision, SpeakerFilterResult)
    assert decision.status == expected_status
    assert len(decision.physical_segment_ids) == 1
    assert decision.physical_segment_ids[0].startswith("pseg_")
    if expected_status == "matched":
        filtered_stop = next(
            output.payload
            for output in outputs
            if output.name == "speech_stopped"
        )
        assert isinstance(filtered_stop, SpeechStopped)
        assert (
            filtered_stop.physical_segment_ids
            == decision.physical_segment_ids
        )
    assert any(
        output.name == "vad_progress"
        and isinstance(output.payload, VadProgress)
        for output in outputs
    )
    assert len(asr.calls) == asr_calls
    assert ("primary_transcript" in {output.name for output in outputs}) is bool(
        asr_calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("search_failure", [None, RuntimeError("qdrant unavailable")])
async def test_vector_search_result_reaches_named_output_without_breaking_asr(
    assets: dict[str, EngineAssetData],
    search_failure: Exception | None,
) -> None:
    update = VadInferencer.Update(boundaries=((0, 8),), processed_samples=8)
    plan_assets = {
        **assets,
        "embedding-engine": _asset(
            "embedding-engine", EngineKind.EMBEDDING, EngineApi.ENGINE_FEATURE,
        ),
    }
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(_vector_definition(), assets=plan_assets),
        inferencers={
            "physical": _Vad([update]),
            "semantic": _Vad([], turns=[True]),
            "asr": _Asr(),
            "embedding": _Embedding(),
        },
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=asyncio.Semaphore(1),
        vectors=_Vectors(failure=search_failure),  # type: ignore[arg-type]
    )

    outputs = [
        output
        async for output in runtime.stream(
            _chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(
                    sample_rate=4,
                    language_code="zh-CN",
                ),
                include_vector_search=True,
            ),
        )
    ]

    assert {output.name for output in outputs} == {"primary_transcript", "vector_search"}
    vector_output = next(output for output in outputs if output.name == "vector_search")
    assert isinstance(vector_output.payload, VectorSearchResult)
    if search_failure is None:
        assert vector_output.payload.error_code is None
        assert vector_output.payload.groups == ({
            "speaker_id": "speaker-1",
            "score": 0.95,
            "observation": {
                "point_id": "point-1",
                "payload": {"speaker_id": "speaker-1"},
            },
        },)
    else:
        assert vector_output.payload.error_code == "vector_search_failed"
        assert vector_output.payload.error_message == "qdrant unavailable"
        assert vector_output.payload.groups == ()


def _dual_definition() -> AudioWorkflowDocument:
    physical_config = PhysicalVadConfig(
        min_segment_seconds=0,
        max_segment_seconds=60,
        audio_buffer_seconds=61,
        speech_pre_roll_seconds=0,
    )
    return AudioWorkflowDocument(
        nodes=[
            PhysicalVadNode(
                id="physical",
                engine_id="vad-engine",
                config=physical_config,
            ),
            AsrNode(id="asr", engine_id="asr-engine"),
            PhysicalVadNode(
                id="interim-physical",
                engine_id="vad-engine",
                config=physical_config,
            ),
            AsrNode(id="interim-asr", engine_id="asr-engine"),
        ],
        edges=[
            AudioEdge(
                id="input-physical",
                source=GraphInputRef(),
                target=NodeInputRef(node_id="physical", port="audio"),
            ),
            AudioEdge(
                id="physical-asr",
                source=NodeOutputRef(node_id="physical", port="segments"),
                target=NodeInputRef(node_id="asr", port="segments"),
            ),
            AudioEdge(
                id="input-interim-physical",
                source=GraphInputRef(),
                target=NodeInputRef(node_id="interim-physical", port="audio"),
            ),
            AudioEdge(
                id="interim-physical-asr",
                source=NodeOutputRef(
                    node_id="interim-physical",
                    port="segments",
                ),
                target=NodeInputRef(node_id="interim-asr", port="segments"),
            ),
        ],
        outputs=[
            NamedOutputBinding(
                name="primary_transcript",
                source=NodeOutputRef(node_id="asr", port="transcripts"),
            ),
            NamedOutputBinding(
                name="interim_transcript",
                source=NodeOutputRef(
                    node_id="interim-asr",
                    port="transcripts",
                ),
            ),
            NamedOutputBinding(
                name="speech_started",
                source=NodeOutputRef(node_id="physical", port="speech_started"),
            ),
            NamedOutputBinding(
                name="speech_stopped",
                source=NodeOutputRef(node_id="physical", port="speech_stopped"),
            ),
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_interim", "expected_names", "expected_preview_calls"),
    [
        (False, ["primary_transcript"], 0),
        (True, ["interim_transcript", "primary_transcript"], 1),
    ],
)
async def test_dual_graph_only_runs_interim_branch_when_requested(
    assets: dict[str, EngineAssetData],
    include_interim: bool,
    expected_names: list[str],
    expected_preview_calls: int,
) -> None:
    update = VadInferencer.Update(
        boundaries=((0, 8),),
        processed_samples=8,
    )
    physical = _Vad([update])
    preview_physical = _Vad([update])
    inferencer = AudioWorkflowRuntime(
        plan=compile_audio_graph(_dual_definition(), assets=assets),
        inferencers={
            "physical": physical,
            "asr": _Asr(),
            "interim-physical": preview_physical,
            "interim-asr": _Asr(),
        },
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=asyncio.Semaphore(2),
    )

    outputs = [
        output
        async for output in inferencer.stream(
            _chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(
                    sample_rate=4,
                    language_code="zh-CN",
                ),
                include_interim_transcripts=include_interim,
            ),
        )
    ]

    assert sorted(output.name for output in outputs) == sorted(expected_names)
    assert preview_physical.stream_calls == expected_preview_calls


@pytest.mark.asyncio
async def test_vad_progress_uses_only_primary_physical_vad_without_binding(
    assets: dict[str, EngineAssetData],
) -> None:
    update = VadInferencer.Update(boundaries=((0, 8),), processed_samples=8)
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(_dual_definition(), assets=assets),
        inferencers={
            "physical": _Vad([update]),
            "asr": _Asr(),
            "interim-physical": _Vad([update]),
            "interim-asr": _Asr(),
        },
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=asyncio.Semaphore(2),
    )

    outputs = [
        output
        async for output in runtime.stream(
            _chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(
                    sample_rate=4,
                    language_code="zh-CN",
                ),
                include_interim_transcripts=True,
                include_vad_events=True,
                include_physical_vad_events=True,
                include_vad_progress=True,
            ),
        )
    ]

    progress = [
        output.payload for output in outputs if output.name == "vad_progress"
    ]
    assert progress == [
        VadProgress(processed_samples=8, sample_rate=4),
        VadProgress(processed_samples=8, sample_rate=4, final=True),
    ]
    physical = [
        output
        for output in outputs
        if output.name
        in {"physical_speech_started", "physical_speech_stopped"}
    ]
    assert [output.name for output in physical] == [
        "physical_speech_started",
        "physical_speech_stopped",
    ]
    assert physical[0].payload.physical_segment_id is not None
    assert (
        physical[1].payload.physical_segment_id
        == physical[0].payload.physical_segment_id
    )


@pytest.mark.asyncio
async def test_vad_progress_can_be_disabled(
    assets: dict[str, EngineAssetData],
) -> None:
    definition = _definition(semantic=False)
    physical = definition.nodes[0]
    assert isinstance(physical, PhysicalVadNode)
    physical.config.progress_interval_seconds = 0
    update = VadInferencer.Update(boundaries=((0, 8),), processed_samples=8)
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(definition, assets=assets),
        inferencers={"physical": _Vad([update]), "asr": _Asr()},
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=asyncio.Semaphore(1),
    )

    outputs = [
        output
        async for output in runtime.stream(
            _chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(
                    sample_rate=4,
                    language_code="zh-CN",
                ),
                include_vad_progress=True,
            ),
        )
    ]

    assert all(output.name != "vad_progress" for output in outputs)


@pytest.mark.asyncio
async def test_final_vad_progress_includes_trailing_silence(
    assets: dict[str, EngineAssetData],
) -> None:
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(_definition(semantic=False), assets=assets),
        inferencers={
            "physical": _Vad(
                [VadInferencer.Update(boundaries=(), processed_samples=4)]
            ),
            "asr": _Asr(),
        },
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=asyncio.Semaphore(1),
    )

    progress = [
        output.payload
        async for output in runtime.stream(
            _chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(
                    sample_rate=4,
                    language_code="zh-CN",
                ),
                include_vad_progress=True,
            ),
        )
        if output.name == "vad_progress"
    ]

    assert progress == [
        VadProgress(processed_samples=4, sample_rate=4),
        VadProgress(processed_samples=8, sample_rate=4, final=True),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remove_punctuation", "transcript", "expected"),
    [
        (
            False,
            "你好， world! “test”—OK… $5 + 🙂",
            "你好， world! “test”—OK… $5 + 🙂",
        ),
        (
            True,
            "你好， world! “test”—OK… $5 + 🙂",
            "你好 world testOK $5 + 🙂",
        ),
        (True, "？！…", ""),
    ],
)
async def test_asr_node_option_removes_unicode_punctuation(
    assets: dict[str, EngineAssetData],
    remove_punctuation: bool,
    transcript: str,
    expected: str,
) -> None:
    definition = _definition(semantic=False)
    asr_node = definition.nodes[-1]
    assert isinstance(asr_node, AsrNode)
    asr_node.config = AsrNodeConfig(remove_punctuation=remove_punctuation)
    update = VadInferencer.Update(
        boundaries=((0, 8),),
        processed_samples=8,
    )
    inferencer = AudioWorkflowRuntime(
        plan=compile_audio_graph(definition, assets=assets),
        inferencers={
            "physical": _Vad([update]),
            "asr": _Asr(transcript),
        },
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=asyncio.Semaphore(1),
    )

    outputs = [
        output
        async for output in inferencer.stream(
            _chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(
                    sample_rate=4,
                    language_code="zh-CN",
                )
            ),
        )
    ]

    assert [output.name for output in outputs] == ["primary_transcript"]
    result = outputs[0].payload
    assert isinstance(result, AsrInferencer.Result)
    assert result.transcript == expected
    assert result.is_final is True
    assert (result.start_sample, result.end_sample) == (0, 8)
    assert result.language_code == "zh-CN"
    assert result.metadata == {"source": "fake"}


@pytest.mark.asyncio
@pytest.mark.parametrize("preview_failure", ["capacity", "vad"])
async def test_dual_graph_preview_failure_does_not_break_primary(
    assets: dict[str, EngineAssetData],
    preview_failure: str,
) -> None:
    update = VadInferencer.Update(
        boundaries=((0, 8),),
        processed_samples=8,
    )
    preview_asr = _Asr()
    inferencer = AudioWorkflowRuntime(
        plan=compile_audio_graph(_dual_definition(), assets=assets),
        inferencers={
            "physical": _Vad([update]),
            "asr": _Asr(),
            "interim-physical": (
                _FailingVad([]) if preview_failure == "vad" else _Vad([update])
            ),
            "interim-asr": preview_asr,
        },
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=(
            _PreviewCapacityLimiter()
            if preview_failure == "capacity"
            else asyncio.Semaphore(2)
        ),  # type: ignore[arg-type]
    )

    outputs = [
        output
        async for output in inferencer.stream(
            _chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(
                    sample_rate=4,
                    language_code="zh-CN",
                ),
                include_interim_transcripts=True,
            ),
        )
    ]

    assert [output.name for output in outputs] == ["primary_transcript"]
    assert preview_asr.calls == []


@pytest.mark.asyncio
async def test_inferencer_merges_semantic_turn_and_emits_named_outputs(
    assets: dict[str, EngineAssetData],
) -> None:
    physical = _Vad(
        [
            VadInferencer.Update(
                boundaries=((0, 4), (4, 8)),
                processed_samples=8,
            )
        ]
    )
    semantic = _Vad([], turns=[False, True])
    asr = _Asr()
    inferencer = AudioWorkflowRuntime(
        plan=compile_audio_graph(_definition(), assets=assets),
        inferencers={"physical": physical, "semantic": semantic, "asr": asr},
        runtime_config=AudioRuntimeConfig(
            segment_queue_capacity=1,
            result_queue_capacity=1,
        ),
        asr_semaphore=asyncio.Semaphore(1),
    )

    outputs = [
        output
        async for output in inferencer.stream(
            _chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(
                    sample_rate=4,
                    language_code="zh-CN",
                ),
                include_vad_events=True,
            ),
        )
    ]

    assert [output.sequence for output in outputs] == list(range(len(outputs)))
    assert [output.name for output in outputs] == [
        "speech_started",
        "speech_started",
        "speech_stopped",
        "primary_transcript",
    ]
    stopped = next(
        output.payload
        for output in outputs
        if output.name == "speech_stopped"
    )
    assert stopped.last_segment_start_sample == 4
    assert stopped.awaiting_physical_stop is True
    assert len(asr.calls) == 1
    assert asr.calls[0][1:3] == (0, 8)


@pytest.mark.asyncio
async def test_inferencer_cancels_all_node_tasks_on_consumer_cancel(
    assets: dict[str, EngineAssetData],
) -> None:
    inferencer = AudioWorkflowRuntime(
        plan=compile_audio_graph(_definition(semantic=False), assets=assets),
        inferencers={"physical": _Vad([]), "asr": _Asr()},
        runtime_config=AudioRuntimeConfig(1, 1),
        asr_semaphore=asyncio.Semaphore(1),
    )
    stream = inferencer.stream(
        _chunks(),
        AudioInferenceRequest(
            recognition=AsrInferencer.Options(
                sample_rate=4,
                language_code="zh-CN",
            )
        ),
    )
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await stream.aclose()
    await asyncio.sleep(0)

    assert not any(
        task.get_name().startswith("audio-graph-") and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_physical_vad_passes_node_postprocessing_to_engine(
    assets: dict[str, EngineAssetData],
) -> None:
    definition = _definition(semantic=False)
    physical = definition.nodes[0]
    assert isinstance(physical, PhysicalVadNode)
    physical.config = PhysicalVadConfig.model_validate(
        {
            **physical.config.model_dump(),
            "postprocessing": {
                "processor": "firered.stream_vad",
                "version": 1,
                "parameters": {"speech_threshold": 0.7},
            },
        }
    )
    vad = _Vad([])
    inferencer = AudioWorkflowRuntime(
        plan=compile_audio_graph(definition, assets=assets),
        inferencers={"physical": vad, "asr": _Asr()},
        runtime_config=AudioRuntimeConfig(1, 1),
        asr_semaphore=asyncio.Semaphore(1),
    )

    assert [
        output
        async for output in inferencer.stream(
            _chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(
                    sample_rate=4,
                    language_code="zh-CN",
                )
            ),
        )
    ] == []
    assert vad.postprocessing is not None
    assert vad.postprocessing["processor"] == "firered.stream_vad"
    assert vad.postprocessing["parameters"]["speech_threshold"] == 0.7
    assert vad.postprocessing["parameters"]["min_silence_frame"] == 20
