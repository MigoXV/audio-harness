from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from audio_harness.inferencers.diarization import (
    DiarizationInferencer,
    DiarizationPostprocessor,
)
from audio_harness.inferencers.embedding import EngineFeatureInferencer
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
from audio_harness.sessions.contracts import (
    DiarizationContext as _DiarizationContext,
    SegmentCandidate as _SegmentCandidate,
    TimelineSpan as _TimelineSpan,
)
from audio_harness.sessions.diarization import (
    DiarizationBatcher,
    DiarizationSession,
)
from audio_harness.sessions.speaker_clustering import SpeakerClusteringSession
from audio_harness.serving.realtime.protocol import (
    AHA_REALTIME_SERVER_EVENT_ADAPTER,
)
from audio_harness.serving.realtime.session import RealtimeTranscriptionSession
from audio_harness.workflows import (
    AsrNode,
    AudioEdge,
    AudioInferenceRequest,
    AudioRuntimeConfig,
    AudioWorkflowDocument,
    AudioWorkflowRuntime,
    FailurePolicy,
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
    VadProgress,
)
from audio_harness.workflows.models import (
    SpeakerClusteringNodeConfig,
    SpeakerClusteringPostprocessingOptions,
    SpeakerDiarizationNodeConfig,
    SpeakerDiarizationPostprocessingOptions,
)
def _event_payload(event) -> dict:
    return (
        event.model_dump(mode="json", exclude_none=True)
        if hasattr(event, "model_dump")
        else event
    )


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


def test_diarization_config_defaults_to_half_second_asr_filter() -> None:
    config = SpeakerDiarizationNodeConfig()

    assert config.min_asr_segment_seconds == 0.5
    assert config.postprocessing == SpeakerDiarizationPostprocessingOptions()


def test_diarization_config_can_disable_postprocessing() -> None:
    config = SpeakerDiarizationNodeConfig(postprocessing=None)

    assert config.postprocessing is None


@pytest.mark.parametrize(
    "postprocessing",
    [
        {"processor": "unknown", "version": 1},
        {
            "processor": "audio_harness.diarization.vad_coverage",
            "version": 2,
        },
        {
            "processor": "audio_harness.diarization.vad_coverage",
            "version": 1,
            "unknown": True,
        },
    ],
)
def test_diarization_config_rejects_invalid_postprocessing(
    postprocessing: dict,
) -> None:
    with pytest.raises(ValidationError):
        SpeakerDiarizationNodeConfig.model_validate(
            {"postprocessing": postprocessing}
        )


def test_diarization_config_rejects_negative_asr_filter() -> None:
    with pytest.raises(ValidationError, match="min_asr_segment_seconds"):
        SpeakerDiarizationNodeConfig(min_asr_segment_seconds=-0.1)


def test_clustering_config_enables_double_threshold_postprocessing_by_default() -> None:
    config = SpeakerClusteringNodeConfig()

    assert config.postprocessing == SpeakerClusteringPostprocessingOptions()
    assert config.postprocessing.high_similarity_threshold == 0.70
    assert config.postprocessing.low_similarity_threshold == 0.50


def test_clustering_config_can_disable_postprocessing() -> None:
    assert SpeakerClusteringNodeConfig(postprocessing=None).postprocessing is None


@pytest.mark.parametrize(
    "postprocessing",
    [
        {"high_similarity_threshold": 1.1},
        {"low_similarity_threshold": -0.1},
        {
            "high_similarity_threshold": 0.6,
            "low_similarity_threshold": 0.7,
        },
    ],
)
def test_clustering_config_rejects_invalid_postprocessing_thresholds(
    postprocessing: dict,
) -> None:
    with pytest.raises(ValidationError):
        SpeakerClusteringNodeConfig(postprocessing=postprocessing)


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


def test_diarization_maps_semantic_audio_across_preserved_gaps() -> None:
    state = batcher(0, 5)
    batch = state.accept(
        _SegmentCandidate(
            audio=bytes((1, 0)) * 4,
            start_sample=10,
            end_sample=22,
            timeline_spans=(
                _TimelineSpan(0, 2, 10),
                _TimelineSpan(2, 4, 20),
            ),
        )
    )[0]

    mapped = batch.map_segments(
        (DiarizationInferencer.Segment(1, 3, 7),)
    )

    assert [(item.start_sample, item.end_sample, item.speaker) for item in mapped] == [
        (11, 12, 7),
        (20, 21, 7),
    ]


def test_postprocessed_diarization_maps_one_turn_across_vad_gaps() -> None:
    state = batcher(0, 5)
    batch = state.accept(
        _SegmentCandidate(
            audio=bytes((1, 0)) * 4,
            start_sample=10,
            end_sample=22,
            timeline_spans=(
                _TimelineSpan(0, 2, 10),
                _TimelineSpan(2, 4, 20),
            ),
        )
    )[0]

    mapped, output = batch.map_processed(
        DiarizationPostprocessor.Segment(0, 4, 7),
        "batch-1",
        min_asr_segment_seconds=0.5,
        batch_completed=True,
    )

    assert mapped is not None
    assert (mapped.start_sample, mapped.end_sample, mapped.speaker) == (10, 22, 7)
    assert output.audio == bytes((1, 0)) * 4
    assert (output.start_sample, output.end_sample) == (10, 22)
    assert [
        (span.input_start_sample, span.input_end_sample, span.session_start_sample)
        for span in output.timeline_spans
    ] == [(0, 2, 10), (2, 4, 20)]
    assert output.diarization is not None
    assert output.diarization.source_completed is True
    assert output.diarization.batch_completed is True


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


class _FourSpeakerDiarization(_Diarization):
    async def stream_diarize(
        self,
        audio: bytes,
        sample_rate: int,
    ) -> AsyncIterator[tuple[DiarizationInferencer.Segment, ...]]:
        del sample_rate
        self.calls.append(audio)
        yield tuple(
            DiarizationInferencer.Segment(index, index + 1, index + 1)
            for index in range(len(audio) // 2)
        )


class _RepeatedSpeakerDiarization(_Diarization):
    async def stream_diarize(
        self,
        audio: bytes,
        sample_rate: int,
    ) -> AsyncIterator[tuple[DiarizationInferencer.Segment, ...]]:
        del sample_rate
        self.calls.append(audio)
        yield tuple(
            DiarizationInferencer.Segment(index, index + 1, index % 2 + 1)
            for index in range(len(audio) // 2)
        )


class _EmptyDiarization(_Diarization):
    async def stream_diarize(
        self,
        audio: bytes,
        sample_rate: int,
    ) -> AsyncIterator[tuple[DiarizationInferencer.Segment, ...]]:
        del sample_rate
        self.calls.append(audio)
        if False:
            yield ()


class _PartialFailureDiarization(_Diarization):
    async def stream_diarize(
        self,
        audio: bytes,
        sample_rate: int,
    ) -> AsyncIterator[tuple[DiarizationInferencer.Segment, ...]]:
        del sample_rate
        self.calls.append(audio)
        yield (DiarizationInferencer.Segment(0, 1, 1),)
        yield (DiarizationInferencer.Segment(2, 3, 2),)
        raise RuntimeError("stream interrupted")


class _SignalingAsr(_Asr):
    def __init__(self, started: asyncio.Event) -> None:
        super().__init__()
        self.started = started

    async def transcribe(
        self,
        audio: bytes,
        *,
        start_sample: int,
        end_sample: int,
        turn_completed: bool,
        options: AsrInferencer.Options,
    ) -> AsyncIterator[AsrInferencer.Result]:
        self.started.set()
        async for result in super().transcribe(
            audio,
            start_sample=start_sample,
            end_sample=end_sample,
            turn_completed=turn_completed,
            options=options,
        ):
            yield result


class _WaitForAsrDiarization(_Diarization):
    def __init__(self, asr_started: asyncio.Event) -> None:
        super().__init__()
        self.asr_started = asr_started

    async def stream_diarize(
        self,
        audio: bytes,
        sample_rate: int,
    ) -> AsyncIterator[tuple[DiarizationInferencer.Segment, ...]]:
        del sample_rate
        self.calls.append(audio)
        yield (DiarizationInferencer.Segment(0, len(audio) // 4, 1),)
        yield (
            DiarizationInferencer.Segment(
                len(audio) // 4,
                len(audio) // 2,
                2,
            ),
        )
        await asyncio.wait_for(self.asr_started.wait(), timeout=1)


class _ClusteringSession:
    def __init__(self) -> None:
        self.started: list[tuple[bytes, ...]] = []
        self.added: list[tuple[bytes, ...]] = []
        self.closed = False

    async def start(self, vectors, *, algorithm, parameters):
        assert algorithm == "hdbscan"
        assert parameters["min_cluster_size"] == 2
        assert parameters["min_samples"] == 2
        assert parameters["cluster_selection_epsilon"] == 0
        assert parameters["allow_single_cluster"] is False
        self.started.append(vectors)
        return tuple(range(len(vectors)))

    async def add(self, vectors):
        self.added.append(vectors)
        total = len(self.started[0]) + sum(len(item) for item in self.added)
        return tuple(reversed(range(total)))

    async def close(self) -> None:
        self.closed = True


class _NoiseClusteringSession(_ClusteringSession):
    async def start(self, vectors, *, algorithm, parameters):
        await super().start(
            vectors,
            algorithm=algorithm,
            parameters=parameters,
        )
        return tuple(-1 for _ in vectors)

    async def add(self, vectors):
        await super().add(vectors)
        total = len(self.started[0]) + sum(len(item) for item in self.added)
        return tuple(-1 for _ in range(total))


class _MixedClusteringSession(_ClusteringSession):
    async def start(self, vectors, *, algorithm, parameters):
        await super().start(
            vectors,
            algorithm=algorithm,
            parameters=parameters,
        )
        assert len(vectors) == 4
        return (0, -1, 0, -1)


class _Embedding(EngineFeatureInferencer):
    def __init__(self, session: _ClusteringSession | None = None) -> None:
        self.audios: list[bytes] = []
        self.session = session or _ClusteringSession()

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
        del algorithm, normalize
        self.audios.append(pcm_s16le)
        vector = np.zeros(expected_dimension, dtype="<f4")
        angle = (len(self.audios) - 1) * 2.0
        vector[0] = np.cos(angle)
        if expected_dimension > 1:
            vector[1] = np.sin(angle)
        return vector.tobytes()

    async def embed(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> tuple[float, ...]:
        vector = await self.embed_bytes(
            pcm_s16le,
            algorithm=algorithm,
            expected_dimension=expected_dimension,
            normalize=normalize,
        )
        return tuple(float(value) for value in np.frombuffer(vector, dtype="<f4"))

    def streaming_clustering(self):
        return self.session


class _FailingEmbedding(_Embedding):
    async def embed_bytes(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> bytes:
        del pcm_s16le, algorithm, expected_dimension, normalize
        raise RuntimeError("embedding unavailable")


class _SimilarEmbedding(_Embedding):
    async def embed_bytes(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> bytes:
        del algorithm, normalize
        self.audios.append(pcm_s16le)
        vector = np.zeros(expected_dimension, dtype="<f4")
        vector[0] = 1
        return vector.tobytes()


class _FailingPostprocessingEmbedding(_Embedding):
    async def embed_bytes(
        self,
        pcm_s16le: bytes,
        *,
        algorithm: str,
        expected_dimension: int,
        normalize: bool = True,
    ) -> bytes:
        if len(self.audios) >= 2:
            raise RuntimeError("postprocessing unavailable")
        return await super().embed_bytes(
            pcm_s16le,
            algorithm=algorithm,
            expected_dimension=expected_dimension,
            normalize=normalize,
        )


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


def _speaker_workflow(
    *,
    min_asr_segment_seconds: float = 0.5,
    postprocessing: bool = True,
) -> AudioWorkflowDocument:
    document = _workflow()
    diarization = next(
        node for node in document.nodes if node.kind == "speaker_diarization"
    )
    diarization = diarization.model_copy(
        update={
            "config": diarization.config.model_copy(
                update={
                    "min_asr_segment_seconds": min_asr_segment_seconds,
                    "postprocessing": (
                        SpeakerDiarizationPostprocessingOptions()
                        if postprocessing
                        else None
                    ),
                }
            )
        }
    )
    return document.model_copy(
        update={
            "nodes": [
                diarization if node.id == diarization.id else node
                for node in document.nodes
            ],
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


def _cluster_workflow(
    *,
    max_batch_seconds: float = 120,
    postprocessing: bool = True,
) -> AudioWorkflowDocument:
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
                        postprocessing=(
                            SpeakerClusteringPostprocessingOptions()
                            if postprocessing
                            else None
                        ),
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
    min_asr_segment_seconds: float = 0.5,
    postprocessing: bool = True,
    asr: _Asr | None = None,
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
    asr = asr or _Asr()
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(
            _speaker_workflow(
                min_asr_segment_seconds=min_asr_segment_seconds,
                postprocessing=postprocessing,
            ),
            assets=assets,
        ),
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
    postprocessing: bool = True,
    embedding: _Embedding | None = None,
):
    clustering_asset = _asset(
        "clustering-engine",
        EngineKind.EMBEDDING,
        EngineApi.ENGINE_FEATURE,
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
    embedding = embedding or _Embedding()
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(
            _cluster_workflow(
                max_batch_seconds=max_batch_seconds,
                postprocessing=postprocessing,
            ),
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
async def test_session_merges_one_speaker_across_vad_gaps_before_asr() -> None:
    async def candidates() -> AsyncIterator[_SegmentCandidate]:
        yield _SegmentCandidate(
            audio=bytes((1, 0)) * 4,
            start_sample=10,
            end_sample=22,
            timeline_spans=(
                _TimelineSpan(0, 2, 10),
                _TimelineSpan(2, 4, 20),
            ),
        )

    session = DiarizationSession(
        SpeakerDiarizationNodeConfig(
            min_batch_seconds=0,
            max_batch_seconds=5,
        ),
        sample_rate=1,
        inferencer=_OneSpeakerDiarization(),
    )

    emissions = [emission async for emission in session.run(candidates())]
    segments = [payload for port, payload in emissions if port == "segments"]
    completed = next(
        payload
        for port, payload in emissions
        if port == "local_results" and payload.phase == "completed"
    )

    assert len(segments) == 1
    assert segments[0].audio == bytes((1, 0)) * 4
    assert (segments[0].start_sample, segments[0].end_sample) == (10, 22)
    assert len(completed.segments) == 1
    assert (
        completed.segments[0].start_sample,
        completed.segments[0].end_sample,
        completed.segments[0].speaker,
    ) == (10, 22, 7)


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
async def test_diarization_submits_stable_speaker_to_asr_before_stream_eof() -> None:
    asr_started = asyncio.Event()
    asr = _SignalingAsr(asr_started)

    outputs, _ = await _run_speaker_chain(
        _WaitForAsrDiarization(asr_started),
        asr=asr,
    )

    assert asr_started.is_set()
    assert len(asr.calls) == 2
    assert any(output.name == "speaker_transcript" for output in outputs)


@pytest.mark.asyncio
async def test_diarization_partial_failure_only_falls_back_uncommitted_tail() -> None:
    outputs, asr = await _run_speaker_chain(_PartialFailureDiarization())
    speaker_results = [
        output.payload
        for output in outputs
        if output.name == "speaker_transcript"
    ]
    failure = next(
        output.payload
        for output in outputs
        if output.name == "diarization_local"
        and output.payload.phase == "failed"
    )

    assert [len(audio) // 2 for audio in asr.calls] == [1, 3]
    assert [result.speaker for result in speaker_results] == [1, None]
    assert [segment.speaker for segment in failure.segments] == [1]


@pytest.mark.asyncio
async def test_diarization_without_labels_falls_back_to_the_full_vad_audio() -> None:
    outputs, asr = await _run_speaker_chain(
        _EmptyDiarization(),
        min_asr_segment_seconds=10,
    )
    speaker_result = next(
        output.payload
        for output in outputs
        if output.name == "speaker_transcript"
    )

    assert [len(audio) // 2 for audio in asr.calls] == [4]
    assert speaker_result.speaker is None
    assert speaker_result.start_sample == 0
    assert speaker_result.end_sample == 4


@pytest.mark.asyncio
async def test_diarization_can_explicitly_use_legacy_eof_processing() -> None:
    outputs, asr = await _run_speaker_chain(
        _Diarization(),
        postprocessing=False,
    )

    assert len(asr.calls) == 2
    assert any(
        output.name == "diarization_local"
        and output.payload.phase == "completed"
        for output in outputs
    )


@pytest.mark.asyncio
async def test_diarization_filters_short_segments_before_asr() -> None:
    outputs, asr = await _run_speaker_chain(
        _Diarization(),
        min_asr_segment_seconds=2.5,
    )
    local_completed = next(
        output.payload
        for output in outputs
        if output.name == "diarization_local"
        and output.payload.phase == "completed"
    )
    primary = next(
        output.payload
        for output in outputs
        if output.name == "primary_transcript"
    )

    assert asr.calls == []
    assert len(local_completed.segments) == 2
    assert not any(
        output.name == "speaker_transcript" for output in outputs
    )
    assert primary.transcript == ""


@pytest.mark.asyncio
async def test_diarization_asr_filter_keeps_segment_at_threshold() -> None:
    _, asr = await _run_speaker_chain(
        _Diarization(),
        min_asr_segment_seconds=2,
    )

    assert len(asr.calls) == 2


@pytest.mark.asyncio
async def test_diarization_failure_passes_original_audio_to_asr() -> None:
    outputs, asr = await _run_speaker_chain(
        _Diarization(RuntimeError("service unavailable")),
        min_asr_segment_seconds=5,
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

    assert len(embedding.audios) == 4
    assert len(embedding.session.started) == 1
    assert len(embedding.session.started[0]) == 2
    assert embedding.session.closed is True
    assert result.phase == "completed"
    assert result.revision == 1
    assert result.replaces_revision is None
    assert [item.speaker_id for item in result.assignments] == [0, 1]
    assert all(item.segment_id.startswith("dseg_") for item in result.assignments)


@pytest.mark.asyncio
async def test_clustering_postprocessing_merges_similar_global_speakers() -> None:
    outputs, embedding = await _run_cluster_chain(
        _Diarization(),
        embedding=_SimilarEmbedding(),
    )
    result = next(
        output.payload
        for output in outputs
        if output.name == "diarization_cluster"
    )

    assert len(embedding.audios) == 4
    assert [item.speaker_id for item in result.assignments] == [0, 0]


@pytest.mark.asyncio
async def test_clustering_can_disable_postprocessing() -> None:
    outputs, embedding = await _run_cluster_chain(
        _Diarization(),
        postprocessing=False,
        embedding=_SimilarEmbedding(),
    )
    result = next(
        output.payload
        for output in outputs
        if output.name == "diarization_cluster"
    )

    assert len(embedding.audios) == 2
    assert [item.speaker_id for item in result.assignments] == [0, 1]


@pytest.mark.asyncio
async def test_clustering_postprocessing_failure_uses_raw_snapshot_when_open() -> None:
    outputs, _ = await _run_cluster_chain(
        _Diarization(),
        embedding=_FailingPostprocessingEmbedding(),
    )
    result = next(
        output.payload
        for output in outputs
        if output.name == "diarization_cluster"
    )

    assert result.phase == "completed"
    assert [item.speaker_id for item in result.assignments] == [0, 1]


@pytest.mark.asyncio
async def test_clustering_postprocessing_failure_honors_fail_pipeline() -> None:
    inferencer = _FailingPostprocessingEmbedding()

    async def candidates() -> AsyncIterator[_SegmentCandidate]:
        for index in range(2):
            yield _SegmentCandidate(
                audio=bytes((index + 1, 0)) * 2,
                start_sample=index * 2,
                end_sample=index * 2 + 2,
                diarization=_DiarizationContext(
                    item_source_id="source-1",
                    source_start_sample=0,
                    source_end_sample=4,
                    source_completed=index == 1,
                    segment_id=f"segment-{index + 1}",
                    batch_id="batch-1",
                    speaker=index + 1,
                    local_speaker_id=f"batch-1:{index + 1}",
                    min_asr_segment_seconds=0,
                    batch_completed=index == 1,
                ),
            )

    session = SpeakerClusteringSession(
        SpeakerClusteringNodeConfig(expected_dimension=2),
        1,
        inferencer,
        failure_policy=FailurePolicy.FAIL_PIPELINE,
    )
    results = session.run(candidates())

    failure = await anext(results)
    assert failure[1].phase == "failed"
    with pytest.raises(RuntimeError, match="postprocessing unavailable"):
        await anext(results)


@pytest.mark.asyncio
async def test_clustering_session_uses_unified_engine_feature_inferencer() -> None:
    inferencer = _Embedding()

    async def candidates() -> AsyncIterator[_SegmentCandidate]:
        for index in range(2):
            yield _SegmentCandidate(
                audio=bytes((index + 1, 0)) * 2,
                start_sample=index * 2,
                end_sample=index * 2 + 2,
                diarization=_DiarizationContext(
                    item_source_id="source-1",
                    source_start_sample=0,
                    source_end_sample=4,
                    source_completed=index == 1,
                    segment_id=f"segment-{index + 1}",
                    batch_id="batch-1",
                    speaker=index + 1,
                    local_speaker_id=f"batch-1:{index + 1}",
                    min_asr_segment_seconds=0,
                    batch_completed=index == 1,
                ),
            )

    session = SpeakerClusteringSession(
        SpeakerClusteringNodeConfig(),
        1,
        inferencer,
        failure_policy=FailurePolicy.FAIL_OPEN,
    )
    results = [payload async for _, payload in session.run(candidates())]

    assert inferencer.audios == [
        b"\x01\x00\x01\x00",
        b"\x02\x00\x02\x00",
        b"\x01\x00\x01\x00",
        b"\x02\x00\x02\x00",
    ]
    assert len(inferencer.session.started) == 1
    assert [item.speaker_id for item in results[0].assignments] == [0, 1]


def test_runtime_rejects_invalid_engine_feature_inferencer() -> None:
    assets = {
        "vad-engine": _asset("vad-engine", EngineKind.VAD, EngineApi.UX_VAD),
        "asr-engine": _asset("asr-engine", EngineKind.ASR, EngineApi.UX_SPEECH),
        "diarization-engine": _asset(
            "diarization-engine",
            EngineKind.DIARIZATION,
            EngineApi.UX_SPEAKER_DIARIZATION,
        ),
        "clustering-engine": _asset(
            "clustering-engine",
            EngineKind.EMBEDDING,
            EngineApi.ENGINE_FEATURE,
        ),
    }

    with pytest.raises(TypeError, match="EngineFeatureInferencer"):
        AudioWorkflowRuntime(
            plan=compile_audio_graph(_cluster_workflow(), assets=assets),
            inferencers={
                "vad": _Vad(),
                "asr": _Asr(),
                "diarization": _Diarization(),
                "clustering": _Vad(),
            },
            runtime_config=AudioRuntimeConfig(4, 32),
            asr_semaphore=asyncio.Semaphore(1),
        )


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
async def test_clustering_assigns_stable_distinct_ids_to_noise() -> None:
    outputs, _ = await _run_cluster_chain(
        _OneSpeakerDiarization(),
        max_batch_seconds=2,
        embedding=_Embedding(_NoiseClusteringSession()),
    )
    results = [
        output.payload
        for output in outputs
        if output.name == "diarization_cluster"
    ]

    assert [item.speaker_id for item in results[0].assignments] == [-1]
    assert [item.speaker_id for item in results[1].assignments] == [-1, -2]


@pytest.mark.asyncio
async def test_clustering_reuses_noise_id_for_local_speaker_segments() -> None:
    outputs, _ = await _run_cluster_chain(
        _RepeatedSpeakerDiarization(),
        embedding=_Embedding(_NoiseClusteringSession()),
    )
    result = next(
        output.payload
        for output in outputs
        if output.name == "diarization_cluster"
    )
    assignments_by_local: dict[str, set[int]] = {}
    for assignment in result.assignments:
        assignments_by_local.setdefault(
            assignment.local_speaker_id,
            set(),
        ).add(assignment.speaker_id)

    assert list(assignments_by_local.values()) == [{-1}, {-2}]


@pytest.mark.asyncio
async def test_clustering_preserves_clusters_and_splits_noise() -> None:
    outputs, _ = await _run_cluster_chain(
        _FourSpeakerDiarization(),
        embedding=_Embedding(_MixedClusteringSession()),
    )
    result = next(
        output.payload
        for output in outputs
        if output.name == "diarization_cluster"
    )

    assert [item.speaker_id for item in result.assignments] == [0, -1, 0, -2]


@pytest.mark.asyncio
async def test_clustering_assigns_distinct_fallback_ids_on_embedding_failure() -> None:
    outputs, _ = await _run_cluster_chain(
        _Diarization(),
        embedding=_FailingEmbedding(),
    )
    result = next(
        output.payload
        for output in outputs
        if output.name == "diarization_cluster"
    )

    assert [item.speaker_id for item in result.assignments] == [-1, -2]


@pytest.mark.asyncio
async def test_realtime_maps_local_diarization_to_versioned_aha_events() -> None:
    sent: list[dict] = []

    async def send(event) -> None:
        sent.append(_event_payload(event))

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
        sent.append(_event_payload(event))

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
        sent.append(_event_payload(event))

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
async def test_realtime_emits_speaker_delta_without_pending_diarization_identity() -> None:
    sent: list[dict] = []

    async def send(event) -> None:
        sent.append(_event_payload(event))

    session = object.__new__(RealtimeTranscriptionSession)
    session._aha_version = "v1"
    session._send_event = send
    session._pending_items = deque()
    session._speaker_item_history = deque(
        [SimpleNamespace(item_id="item-1", start_sample=0, end_sample=100)]
    )
    result = SpeakerTranscriptionResult(
        phase="delta",
        item_source_id="source-1",
        segment_id="dseg-1",
        batch_id="batch-1",
        local_speaker_id=None,
        speaker=None,
        source_start_sample=0,
        source_end_sample=100,
        start_sample=10,
        end_sample=50,
        sample_rate=10,
        text="你",
        language_code="zh-CN",
    )

    await session._handle_workflow_event("speaker_transcript", result)

    assert sent[0] == {
        "type": "x_aha.input_audio_transcription.speaker.delta",
        "event_id": sent[0]["event_id"],
        "item_id": "item-1",
        "item_source_id": "source-1",
        "segment_id": "dseg-1",
        "batch_id": "batch-1",
        "audio_start_ms": 1000,
        "audio_end_ms": 5000,
        "language": "zh-CN",
        "delta": "你",
    }
    AHA_REALTIME_SERVER_EVENT_ADAPTER.validate_python(sent[0])


@pytest.mark.asyncio
async def test_realtime_cluster_completed_is_a_full_snapshot() -> None:
    sent: list[dict] = []

    async def send(event) -> None:
        sent.append(_event_payload(event))

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


@pytest.mark.asyncio
async def test_realtime_maps_normal_and_final_vad_progress() -> None:
    sent: list[dict] = []

    async def send(event) -> None:
        sent.append(_event_payload(event))

    session = object.__new__(RealtimeTranscriptionSession)
    session._aha_version = "v1"
    session._send_event = send

    await session._handle_workflow_event(
        "vad_progress",
        VadProgress(processed_samples=1_600, sample_rate=16_000),
    )
    await session._handle_workflow_event(
        "vad_progress",
        VadProgress(
            processed_samples=3_004_240,
            sample_rate=16_000,
            final=True,
        ),
    )

    assert [event["processed_audio_ms"] for event in sent] == [100, 187_765]
    assert "final" not in sent[0]
    assert sent[1]["final"] is True


@pytest.mark.asyncio
async def test_realtime_final_cluster_snapshot_advances_revision_and_duration() -> None:
    sent: list[dict] = []

    async def send(event) -> None:
        sent.append(_event_payload(event))

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
    session = object.__new__(RealtimeTranscriptionSession)
    session._aha_version = "v1"
    session._send_event = send
    session._cluster_output_enabled = True
    session._discard_workflow_outputs = False
    session._workflow_failure = None
    session._cluster_latest_result = result
    session._cluster_last_completed = result

    await session._emit_final_cluster_snapshot(3_004_240)

    assert sent == [
        {
            "type": "x_aha.diarization.cluster.completed",
            "event_id": sent[0]["event_id"],
            "revision": 4,
            "replaces_revision": 3,
            "final": True,
            "processed_audio_ms": 187_765,
            "full_snapshot": True,
            "assignments": [
                {
                    "segment_id": "dseg-1",
                    "local_speaker_id": "batch-1:2",
                    "speaker_id": 1,
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_realtime_final_cluster_snapshot_can_be_empty() -> None:
    sent: list[dict] = []

    async def send(event) -> None:
        sent.append(_event_payload(event))

    session = object.__new__(RealtimeTranscriptionSession)
    session._aha_version = "v1"
    session._send_event = send
    session._cluster_output_enabled = True
    session._discard_workflow_outputs = False
    session._workflow_failure = None
    session._cluster_latest_result = None
    session._cluster_last_completed = None

    await session._emit_final_cluster_snapshot(16_000)

    assert sent[0]["type"] == "x_aha.diarization.cluster.completed"
    assert sent[0]["revision"] == 1
    assert sent[0]["replaces_revision"] is None
    assert sent[0]["processed_audio_ms"] == 1_000
    assert sent[0]["final"] is True
    assert sent[0]["full_snapshot"] is True
    assert sent[0]["assignments"] == []


@pytest.mark.asyncio
async def test_realtime_final_cluster_snapshot_preserves_cluster_failure() -> None:
    sent: list[dict] = []

    async def send(event) -> None:
        sent.append(_event_payload(event))

    completed = DiarizationClusterResult(
        phase="completed",
        revision=2,
        replaces_revision=1,
        assignments=(),
    )
    failed = DiarizationClusterResult(
        phase="failed",
        revision=3,
        replaces_revision=2,
        assignments=(),
        error_code="cluster_rpc_failed",
        error_message="unavailable",
    )
    session = object.__new__(RealtimeTranscriptionSession)
    session._aha_version = "v1"
    session._send_event = send
    session._cluster_output_enabled = True
    session._discard_workflow_outputs = False
    session._workflow_failure = None
    session._cluster_latest_result = failed
    session._cluster_last_completed = completed

    await session._emit_final_cluster_snapshot(8_000)

    assert sent[0]["type"] == "x_aha.diarization.cluster.failed"
    assert sent[0]["revision"] == 4
    assert sent[0]["replaces_revision"] == 2
    assert sent[0]["processed_audio_ms"] == 500
    assert sent[0]["final"] is True
    assert sent[0]["error"] == {
        "code": "cluster_rpc_failed",
        "message": "unavailable",
    }
    assert "assignments" not in sent[0]


@pytest.mark.asyncio
async def test_finish_workflow_waits_for_outputs_before_final_cluster() -> None:
    order: list[str] = []
    finished = asyncio.Event()

    class Transcription:
        processed_samples = 16_000

        async def finish(self) -> None:
            order.append("finish")
            finished.set()

    async def drain_outputs() -> None:
        await finished.wait()
        order.append("vad-final")

    async def send(event) -> None:
        if _event_payload(event)["type"] == "x_aha.diarization.cluster.completed":
            order.append("cluster-final")

    session = object.__new__(RealtimeTranscriptionSession)
    session._aha_version = "v1"
    session._send_event = send
    session._transcription = Transcription()
    session._workflow_task = asyncio.create_task(drain_outputs())
    session._session_context = None
    session._reservation = None
    session._cluster_output_enabled = True
    session._discard_workflow_outputs = False
    session._workflow_failure = None
    session._cluster_latest_result = None
    session._cluster_last_completed = None

    await session._finish_workflow(send_results=True)

    assert order == ["finish", "vad-final", "cluster-final"]


@pytest.mark.asyncio
async def test_finish_workflow_emits_final_cluster_failure_when_finish_fails() -> None:
    sent: list[dict] = []

    class Transcription:
        processed_samples = 8_000

        async def finish(self) -> None:
            raise RuntimeError("flush failed")

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    async def send(event) -> None:
        sent.append(_event_payload(event))

    session = object.__new__(RealtimeTranscriptionSession)
    session._aha_version = "v1"
    session._send_event = send
    session._transcription = Transcription()
    session._workflow_task = asyncio.create_task(wait_forever())
    session._session_context = None
    session._reservation = None
    session._cluster_output_enabled = True
    session._discard_workflow_outputs = False
    session._workflow_failure = None
    session._cluster_latest_result = None
    session._cluster_last_completed = None

    with pytest.raises(RuntimeError, match="flush failed"):
        await session._finish_workflow(send_results=True)

    assert sent[0]["type"] == "x_aha.diarization.cluster.failed"
    assert sent[0]["final"] is True
    assert sent[0]["processed_audio_ms"] == 500
    assert sent[0]["error"] == {
        "code": "workflow_failed",
        "message": "flush failed",
    }
