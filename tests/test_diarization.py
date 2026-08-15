from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from audio_harness.inferencers.diarization import DiarizationInferencer
from audio_harness.inferencers.embedding import AudioEmbeddingInferencer
from audio_harness.engines.models import (
    EngineApi,
    EngineAssetData,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.inferencers.asr import AsrInferencer
from audio_harness.inferencers.vad import VadInferencer
from audio_harness.media.pcm import PcmChunk
from audio_harness.serving.realtime.session import RealtimeTranscriptionSession
from audio_harness.workflows import (
    AsrNode,
    AudioEdge,
    AudioInferenceRequest,
    AudioRuntimeConfig,
    AudioWorkflowDocument,
    AudioWorkflowRuntime,
    GraphInputRef,
    NamedOutputBinding,
    NodeInputRef,
    NodeOutputRef,
    PhysicalVadConfig,
    PhysicalVadNode,
    SpeakerDiarizationNode,
    SpeakerClusteringNode,
    compile_audio_graph,
)
from audio_harness.workflows.events import (
    DiarizationClusterAssignment,
    DiarizationClusterResult,
    DiarizationLocalResult,
    DiarizationSegment,
    SpeakerTranscriptionResult,
)
from audio_harness.workflows.models import (
    SpeakerClusteringNodeConfig,
    SpeakerDiarizationNodeConfig,
)
from audio_harness.workflows.runtime import DiarizationBatcher, _SegmentCandidate


def candidate(start: int, samples: int, value: int = 1) -> _SegmentCandidate:
    return _SegmentCandidate(
        audio=bytes((value, 0)) * samples,
        start_sample=start,
        end_sample=start + samples,
    )


def batcher(minimum: float, maximum: float) -> DiarizationBatcher:
    return DiarizationBatcher(
        SpeakerDiarizationNodeConfig(
            min_batch_seconds=minimum,
            max_batch_seconds=maximum,
        ),
        sample_rate=1,
    )


def test_diarization_config_rejects_minimum_above_maximum() -> None:
    with pytest.raises(ValidationError, match="min_batch_seconds"):
        SpeakerDiarizationNodeConfig(
            min_batch_seconds=121,
            max_batch_seconds=120,
        )


def test_diarization_batcher_accumulates_until_minimum() -> None:
    state = batcher(3, 5)
    assert state.accept(candidate(0, 2)) == ()
    emitted = state.accept(candidate(10, 2, 2))

    assert len(emitted) == 1
    assert emitted[0].samples == 4
    assert emitted[0].audio == bytes((1, 0)) * 2 + bytes((2, 0)) * 2
    assert state.finish() == ()


def test_diarization_batcher_flushes_old_part_before_overflow_without_loss() -> None:
    state = batcher(5, 5)
    assert state.accept(candidate(0, 4)) == ()

    emitted = state.accept(candidate(10, 2, 2))
    tail = state.finish()

    assert [batch.samples for batch in emitted] == [4]
    assert [batch.samples for batch in tail] == [2]
    assert emitted[0].audio + tail[0].audio == (
        bytes((1, 0)) * 4 + bytes((2, 0)) * 2
    )


def test_diarization_batcher_hard_splits_oversized_block_and_flushes_tail() -> None:
    state = batcher(3, 5)
    emitted = state.accept(candidate(20, 12))
    tail = state.finish()

    assert [batch.samples for batch in emitted] == [5, 5]
    assert [batch.samples for batch in tail] == [2]
    assert [batch.start_sample for batch in (*emitted, *tail)] == [20, 25, 30]
    spans = [batch.spans[0] for batch in (*emitted, *tail)]
    assert len({span.source_id for span in spans}) == 1
    assert [span.source_completed for span in spans] == [False, False, True]


def test_zero_minimum_sends_each_vad_block_and_still_hard_splits() -> None:
    state = batcher(0, 5)
    first = state.accept(candidate(0, 2))
    oversized = state.accept(candidate(10, 7))

    assert [batch.samples for batch in first] == [2]
    assert [batch.samples for batch in oversized] == [5, 2]
    assert state.finish() == ()


def test_diarization_result_is_split_and_mapped_across_vad_gaps() -> None:
    state = batcher(4, 5)
    assert state.accept(candidate(10, 2)) == ()
    emitted = state.accept(candidate(20, 2))

    mapped = emitted[0].map_segments(
        (DiarizationInferencer.Segment(1, 3, 7),)
    )

    assert [(item.start_sample, item.end_sample, item.speaker) for item in mapped] == [
        (11, 12, 7),
        (20, 21, 7),
    ]


def test_diarization_mapping_clamps_invalid_service_ranges() -> None:
    state = batcher(0, 5)
    batch = state.accept(candidate(10, 2))[0]

    mapped = batch.map_segments(
        (
            DiarizationInferencer.Segment(-2, 1, 1),
            DiarizationInferencer.Segment(2, 8, 2),
            DiarizationInferencer.Segment(2, 1, 3),
        )
    )

    assert [(item.start_sample, item.end_sample, item.speaker) for item in mapped] == [
        (10, 11, 1),
    ]


class _Vad(VadInferencer):
    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass

    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
        *,
        postprocessing=None,
    ) -> AsyncIterator[VadInferencer.Update]:
        del sample_rate, postprocessing
        samples = 0
        async for chunk in audio_chunks:
            samples += len(chunk) // 2
        yield VadInferencer.Update(((0, samples),), samples)

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        del audio, sample_rate
        return True


class _Asr(AsrInferencer):
    def __init__(self) -> None:
        self.calls: list[bytes] = []

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass

    async def transcribe(
        self,
        audio: bytes,
        *,
        start_sample: int,
        end_sample: int,
        turn_completed: bool,
        options: AsrInferencer.Options,
    ) -> AsyncIterator[AsrInferencer.Result]:
        self.calls.append(audio)
        yield AsrInferencer.Result(
            "ok",
            True,
            start_sample,
            end_sample,
            options.sample_rate,
            turn_completed,
        )


class _Diarization(DiarizationInferencer):
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[bytes] = []

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass

    async def stream_diarize(
        self,
        audio: bytes,
        sample_rate: int,
    ) -> AsyncIterator[tuple[DiarizationInferencer.Segment, ...]]:
        del sample_rate
        self.calls.append(audio)
        if self.failure is not None:
            raise self.failure
        yield (DiarizationInferencer.Segment(0, len(audio) // 4, 1),)
        yield (
            DiarizationInferencer.Segment(
                len(audio) // 4,
                len(audio) // 2,
                2,
            ),
        )


class _OneSpeakerDiarization(_Diarization):
    async def stream_diarize(
        self,
        audio: bytes,
        sample_rate: int,
    ) -> AsyncIterator[tuple[DiarizationInferencer.Segment, ...]]:
        del sample_rate
        self.calls.append(audio)
        yield (DiarizationInferencer.Segment(0, len(audio) // 2, 7),)


class _ClusteringSession:
    def __init__(self) -> None:
        self.started: list[tuple[bytes, ...]] = []
        self.added: list[tuple[bytes, ...]] = []

    async def start(self, vectors, *, algorithm, parameters):
        assert algorithm == "hdbscan"
        assert parameters["min_cluster_size"] == 2
        self.started.append(vectors)
        return tuple(range(len(vectors)))

    async def add(self, vectors):
        self.added.append(vectors)
        total = len(self.started[0]) + sum(len(item) for item in self.added)
        return tuple(reversed(range(total)))

    async def close(self) -> None:
        pass


class _Embedding(AudioEmbeddingInferencer):
    def __init__(self) -> None:
        self.audios: list[bytes] = []
        self.session = _ClusteringSession()

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass

    async def embed_bytes(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> bytes:
        del algorithm, expected_dimension, normalize
        self.audios.append(pcm_s16le)
        return bytes([len(self.audios)]) * 8

    def streaming_clustering(self):
        return self.session


def _asset(
    asset_id: str,
    kind: EngineKind,
    api: EngineApi,
) -> EngineAssetData:
    now = datetime.now(timezone.utc)
    return EngineAssetData(
        id=asset_id,
        slug=asset_id,
        name=asset_id,
        kind=kind,
        api=api,
        protocol=EngineProtocol.GRPC,
        target="127.0.0.1:1",
        connect_timeout_seconds=1,
        request_timeout_seconds=None,
        enabled=True,
        status=EngineStatus.HEALTHY,
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


def _workflow() -> AudioWorkflowDocument:
    return AudioWorkflowDocument(
        nodes=[
            PhysicalVadNode(
                id="vad",
                engine_id="vad-engine",
                config=PhysicalVadConfig(
                    min_segment_seconds=0,
                    max_segment_seconds=60,
                    audio_buffer_seconds=60,
                    speech_pre_roll_seconds=0,
                ),
            ),
            AsrNode(id="asr", engine_id="asr-engine"),
            SpeakerDiarizationNode(
                id="diarization",
                engine_id="diarization-engine",
                config=SpeakerDiarizationNodeConfig(
                    min_batch_seconds=0,
                    max_batch_seconds=120,
                ),
            ),
        ],
        edges=[
            AudioEdge(
                id="input-vad",
                source=GraphInputRef(),
                target=NodeInputRef(node_id="vad", port="audio"),
            ),
            AudioEdge(
                id="vad-asr",
                source=NodeOutputRef(node_id="vad", port="segments"),
                target=NodeInputRef(node_id="asr", port="segments"),
            ),
            AudioEdge(
                id="vad-diarization",
                source=NodeOutputRef(node_id="vad", port="segments"),
                target=NodeInputRef(node_id="diarization", port="segments"),
            ),
        ],
        outputs=[
            NamedOutputBinding(
                name="primary_transcript",
                source=NodeOutputRef(node_id="asr", port="transcripts"),
            ),
            NamedOutputBinding(
                name="speech_started",
                source=NodeOutputRef(node_id="vad", port="speech_started"),
            ),
            NamedOutputBinding(
                name="speech_stopped",
                source=NodeOutputRef(node_id="vad", port="speech_stopped"),
            ),
            NamedOutputBinding(
                name="diarization_local",
                source=NodeOutputRef(
                    node_id="diarization",
                    port="local_results",
                ),
            ),
        ],
    )


def _speaker_workflow() -> AudioWorkflowDocument:
    document = _workflow()
    return document.model_copy(
        update={
            "edges": [
                edge
                for edge in document.edges
                if edge.id != "vad-asr"
            ]
            + [
                AudioEdge(
                    id="diarization-asr",
                    source=NodeOutputRef(
                        node_id="diarization",
                        port="segments",
                    ),
                    target=NodeInputRef(node_id="asr", port="segments"),
                )
            ],
            "outputs": document.outputs
            + [
                NamedOutputBinding(
                    name="speaker_transcript",
                    source=NodeOutputRef(
                        node_id="asr",
                        port="speaker_transcripts",
                    ),
                )
            ],
        }
    )


def _cluster_workflow(*, max_batch_seconds: float = 120) -> AudioWorkflowDocument:
    document = _speaker_workflow()
    diarization = next(
        node for node in document.nodes if node.kind == "speaker_diarization"
    )
    diarization = diarization.model_copy(
        update={
            "config": SpeakerDiarizationNodeConfig(
                min_batch_seconds=0,
                max_batch_seconds=max_batch_seconds,
            )
        }
    )
    return document.model_copy(
        update={
            "nodes": [
                diarization if node.id == diarization.id else node
                for node in document.nodes
            ]
            + [
                SpeakerClusteringNode(
                    id="clustering",
                    engine_id="clustering-engine",
                    config=SpeakerClusteringNodeConfig(
                        expected_dimension=2,
                    ),
                )
            ],
            "edges": document.edges
            + [
                AudioEdge(
                    id="diarization-clustering",
                    source=NodeOutputRef(
                        node_id="diarization",
                        port="segments",
                    ),
                    target=NodeInputRef(
                        node_id="clustering",
                        port="segments",
                    ),
                )
            ],
            "outputs": document.outputs
            + [
                NamedOutputBinding(
                    name="diarization_cluster",
                    source=NodeOutputRef(
                        node_id="clustering",
                        port="results",
                    ),
                )
            ],
        }
    )


async def _audio() -> AsyncIterator[PcmChunk]:
    yield PcmChunk(b"\x00\x00" * 4, sample_rate=1, start_sample=0)


async def _run_diarization(
    inferencer: _Diarization,
    *,
    enabled: bool,
):
    assets = {
        "vad-engine": _asset("vad-engine", EngineKind.VAD, EngineApi.UX_VAD),
        "asr-engine": _asset("asr-engine", EngineKind.ASR, EngineApi.UX_SPEECH),
        "diarization-engine": _asset(
            "diarization-engine",
            EngineKind.DIARIZATION,
            EngineApi.UX_SPEAKER_DIARIZATION,
        ),
    }
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(_workflow(), assets=assets),
        inferencers={
            "vad": _Vad(),
            "asr": _Asr(),
            "diarization": inferencer,
        },
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=asyncio.Semaphore(1),
    )
    return [
        output
        async for output in runtime.stream(
            _audio(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(1, "zh-CN"),
                include_diarization=enabled,
            ),
        )
    ]


async def _run_speaker_chain(
    diarization: _Diarization,
    *,
    include_diarization: bool = True,
) -> tuple[list, _Asr]:
    assets = {
        "vad-engine": _asset("vad-engine", EngineKind.VAD, EngineApi.UX_VAD),
        "asr-engine": _asset("asr-engine", EngineKind.ASR, EngineApi.UX_SPEECH),
        "diarization-engine": _asset(
            "diarization-engine",
            EngineKind.DIARIZATION,
            EngineApi.UX_SPEAKER_DIARIZATION,
        ),
    }
    asr = _Asr()
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(_speaker_workflow(), assets=assets),
        inferencers={
            "vad": _Vad(),
            "asr": asr,
            "diarization": diarization,
        },
        runtime_config=AudioRuntimeConfig(2, 16),
        asr_semaphore=asyncio.Semaphore(1),
    )
    outputs = [
        output
        async for output in runtime.stream(
            _audio(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(1, "zh-CN"),
                include_diarization=include_diarization,
            ),
        )
    ]
    return outputs, asr


async def _run_cluster_chain(
    diarization: _Diarization,
    *,
    max_batch_seconds: float = 120,
):
    clustering_asset = _asset(
        "clustering-engine",
        EngineKind.EMBEDDING,
        EngineApi.ENGINE_FEATURE,
    )
    clustering_asset = clustering_asset.__class__(
        **{
            **clustering_asset.__dict__,
            "details": {"capabilities": ["feature", "streaming_clustering"]},
        }
    )
    assets = {
        "vad-engine": _asset("vad-engine", EngineKind.VAD, EngineApi.UX_VAD),
        "asr-engine": _asset("asr-engine", EngineKind.ASR, EngineApi.UX_SPEECH),
        "diarization-engine": _asset(
            "diarization-engine",
            EngineKind.DIARIZATION,
            EngineApi.UX_SPEAKER_DIARIZATION,
        ),
        "clustering-engine": clustering_asset,
    }
    embedding = _Embedding()
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(
            _cluster_workflow(max_batch_seconds=max_batch_seconds),
            assets=assets,
        ),
        inferencers={
            "vad": _Vad(),
            "asr": _Asr(),
            "diarization": diarization,
            "clustering": embedding,
        },
        runtime_config=AudioRuntimeConfig(4, 32),
        asr_semaphore=asyncio.Semaphore(1),
    )
    outputs = [
        output
        async for output in runtime.stream(
            _audio(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(1, "zh-CN"),
                include_diarization=True,
            ),
        )
    ]
    return outputs, embedding


@pytest.mark.asyncio
async def test_runtime_streams_diarization_deltas_and_complete_result() -> None:
    inferencer = _Diarization()
    outputs = await _run_diarization(inferencer, enabled=True)
    local = [
        output.payload
        for output in outputs
        if output.name == "diarization_local"
    ]

    assert [result.phase for result in local] == ["delta", "delta", "completed"]
    assert all(isinstance(result, DiarizationLocalResult) for result in local)
    assert local[0].batch_id == local[-1].batch_id
    assert [result.sequence for result in local] == [0, 1, None]
    assert [segment.speaker for segment in local[-1].segments] == [1, 2]
    assert [segment.segment_id for segment in local[-1].segments] == [
        local[0].segments[0].segment_id,
        local[1].segments[0].segment_id,
    ]
    assert [segment.local_speaker_id for segment in local[-1].segments] == [
        f"{local[-1].batch_id}:1",
        f"{local[-1].batch_id}:2",
    ]
    assert len(inferencer.calls) == 1


@pytest.mark.asyncio
async def test_runtime_diarization_is_opt_in_and_failure_is_fail_open() -> None:
    disabled = _Diarization()
    outputs = await _run_diarization(disabled, enabled=False)
    assert disabled.calls == []
    assert [output.name for output in outputs] == ["primary_transcript"]

    failed = _Diarization(RuntimeError("service unavailable"))
    outputs = await _run_diarization(failed, enabled=True)
    names = [output.name for output in outputs]
    failure = next(
        output.payload
        for output in outputs
        if output.name == "diarization_local"
    )
    assert "primary_transcript" in names
    assert failure.phase == "failed"
    assert failure.error_code == "diarization_failed"
    assert failure.error_message == "service unavailable"


@pytest.mark.asyncio
async def test_diarization_can_feed_asr_and_aggregate_the_source_turn() -> None:
    outputs, asr = await _run_speaker_chain(_Diarization())
    speaker_results = [
        output.payload
        for output in outputs
        if output.name == "speaker_transcript"
    ]
    primary = [
        output.payload
        for output in outputs
        if output.name == "primary_transcript"
    ]
    local_completed = next(
        output.payload
        for output in outputs
        if output.name == "diarization_local"
        and output.payload.phase == "completed"
    )

    assert len(asr.calls) == 2
    assert [result.phase for result in speaker_results] == [
        "completed",
        "completed",
    ]
    assert [result.speaker for result in speaker_results] == [1, 2]
    assert [result.segment_id for result in speaker_results] == [
        segment.segment_id for segment in local_completed.segments
    ]
    assert len(primary) == 1
    assert primary[0].transcript == "ok ok"


@pytest.mark.asyncio
async def test_diarization_failure_passes_original_audio_to_asr() -> None:
    outputs, asr = await _run_speaker_chain(
        _Diarization(RuntimeError("service unavailable"))
    )
    speaker = next(
        output.payload
        for output in outputs
        if output.name == "speaker_transcript"
    )
    primary = next(
        output.payload
        for output in outputs
        if output.name == "primary_transcript"
    )

    assert asr.calls == [b"\x00\x00" * 4]
    assert speaker.phase == "completed"
    assert speaker.speaker is None
    assert speaker.local_speaker_id is None
    assert primary.transcript == "ok"


@pytest.mark.asyncio
async def test_upstream_diarization_runs_without_aha_outputs() -> None:
    diarization = _Diarization()
    outputs, asr = await _run_speaker_chain(
        diarization,
        include_diarization=False,
    )

    assert len(diarization.calls) == 1
    assert len(asr.calls) == 2
    assert [output.name for output in outputs] == ["primary_transcript"]


@pytest.mark.asyncio
async def test_clustering_groups_local_speakers_and_emits_full_snapshot() -> None:
    outputs, embedding = await _run_cluster_chain(_Diarization())
    result = next(
        output.payload
        for output in outputs
        if output.name == "diarization_cluster"
    )

    assert len(embedding.audios) == 2
    assert len(embedding.session.started) == 1
    assert len(embedding.session.started[0]) == 2
    assert result.phase == "completed"
    assert result.revision == 1
    assert result.replaces_revision is None
    assert [item.speaker_id for item in result.assignments] == [0, 1]
    assert all(item.segment_id.startswith("dseg_") for item in result.assignments)


@pytest.mark.asyncio
async def test_clustering_single_vector_is_noise_without_remote_stream() -> None:
    outputs, embedding = await _run_cluster_chain(_OneSpeakerDiarization())
    result = next(
        output.payload
        for output in outputs
        if output.name == "diarization_cluster"
    )

    assert len(embedding.audios) == 1
    assert embedding.session.started == []
    assert [item.speaker_id for item in result.assignments] == [-1]


@pytest.mark.asyncio
async def test_clustering_second_batch_replaces_the_full_noise_snapshot() -> None:
    outputs, embedding = await _run_cluster_chain(
        _OneSpeakerDiarization(),
        max_batch_seconds=2,
    )
    results = [
        output.payload
        for output in outputs
        if output.name == "diarization_cluster"
    ]

    assert [result.revision for result in results] == [1, 2]
    assert [result.replaces_revision for result in results] == [None, 1]
    assert [item.speaker_id for item in results[0].assignments] == [-1]
    assert [item.speaker_id for item in results[1].assignments] == [0, 1]
    assert len(embedding.session.started[0]) == 2


@pytest.mark.asyncio
async def test_realtime_maps_local_diarization_to_versioned_aha_events() -> None:
    sent: list[dict] = []

    async def send(event) -> None:
        sent.append(event)

    session = object.__new__(RealtimeTranscriptionSession)
    session._aha_version = "v1"
    session._send_event = send
    result = DiarizationLocalResult(
        phase="completed",
        batch_id="batch-1",
        sequence=None,
        batch_start_sample=16_000,
        batch_end_sample=64_000,
        speech_samples=32_000,
        sample_rate=16_000,
        segments=(
            # speaker 编号只在 batch-1 内有效。
            DiarizationSegment(
                "dseg-1",
                16_000,
                32_000,
                2,
                "batch-1:2",
            ),
        ),
        latency_ms=12.3456,
    )

    await session._handle_workflow_event("diarization_local", result)

    assert sent[0] == {
        "type": "x_aha.diarization.local.completed",
        "event_id": sent[0]["event_id"],
        "batch_id": "batch-1",
        "batch_audio_start_ms": 1000,
        "batch_audio_end_ms": 4000,
        "speech_duration_ms": 2000,
        "segments": [
            {
                "segment_id": "dseg-1",
                "audio_start_ms": 1000,
                "audio_end_ms": 2000,
                "speaker": 2,
                "local_speaker_id": "batch-1:2",
            }
        ],
        "latency_ms": 12.346,
    }
    assert "item_id" not in sent[0]


@pytest.mark.asyncio
async def test_realtime_does_not_emit_diarization_without_aha_v1() -> None:
    sent: list[dict] = []

    async def send(event) -> None:
        sent.append(event)

    session = object.__new__(RealtimeTranscriptionSession)
    session._aha_version = None
    session._send_event = send
    result = DiarizationLocalResult(
        phase="failed",
        batch_id="batch-1",
        sequence=None,
        batch_start_sample=0,
        batch_end_sample=1,
        speech_samples=1,
        sample_rate=1,
        segments=(),
        latency_ms=1,
        error_code="diarization_failed",
        error_message="failed",
    )

    await session._handle_workflow_event("diarization_local", result)

    assert sent == []


@pytest.mark.asyncio
async def test_realtime_emits_speaker_transcript_with_matching_item() -> None:
    sent: list[dict] = []

    async def send(event) -> None:
        sent.append(event)

    session = object.__new__(RealtimeTranscriptionSession)
    session._aha_version = "v1"
    session._send_event = send
    session._pending_items = deque(
        [SimpleNamespace(item_id="item-1", start_sample=0, end_sample=100)]
    )
    result = SpeakerTranscriptionResult(
        phase="completed",
        item_source_id="source-1",
        segment_id="dseg-1",
        batch_id="batch-1",
        local_speaker_id="batch-1:2",
        speaker=2,
        source_start_sample=0,
        source_end_sample=100,
        start_sample=10,
        end_sample=50,
        sample_rate=10,
        text="你好",
        language_code="zh-CN",
    )

    await session._handle_workflow_event("speaker_transcript", result)

    assert sent[0] == {
        "type": "x_aha.input_audio_transcription.speaker.completed",
        "event_id": sent[0]["event_id"],
        "item_id": "item-1",
        "item_source_id": "source-1",
        "segment_id": "dseg-1",
        "batch_id": "batch-1",
        "local_speaker_id": "batch-1:2",
        "speaker": 2,
        "audio_start_ms": 1000,
        "audio_end_ms": 5000,
        "language": "zh-CN",
        "transcript": "你好",
    }


@pytest.mark.asyncio
async def test_realtime_cluster_completed_is_a_full_snapshot() -> None:
    sent: list[dict] = []

    async def send(event) -> None:
        sent.append(event)

    session = object.__new__(RealtimeTranscriptionSession)
    session._aha_version = "v1"
    session._send_event = send
    result = DiarizationClusterResult(
        phase="completed",
        revision=3,
        replaces_revision=2,
        assignments=(
            DiarizationClusterAssignment(
                segment_id="dseg-1",
                local_speaker_id="batch-1:2",
                speaker_id=1,
            ),
        ),
    )

    await session._handle_workflow_event("diarization_cluster", result)

    assert sent[0]["type"] == "x_aha.diarization.cluster.completed"
    assert sent[0]["revision"] == 3
    assert sent[0]["replaces_revision"] == 2
    assert sent[0]["full_snapshot"] is True
    assert sent[0]["assignments"] == [
        {
            "segment_id": "dseg-1",
            "local_speaker_id": "batch-1:2",
            "speaker_id": 1,
        }
    ]
