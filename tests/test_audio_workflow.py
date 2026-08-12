from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from audio_harness.assets import (
    EngineApi,
    EngineAssetData,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.domain import (
    AudioSegment,
    PcmChunk,
    RecognitionOptions,
    TranscriptionResult,
    VadBoundary,
    VadPostprocessingOptions,
    VadUpdate,
)
from audio_harness.workflows import (
    AsrNode,
    AudioEdge,
    AudioGraphValidationError,
    AudioInferenceRequest,
    AudioRuntimeConfig,
    AudioWorkflowDocument,
    AudioWorkflowInferencer,
    FailurePolicy,
    GraphInputRef,
    NamedOutputBinding,
    NodeInputRef,
    NodeOutputRef,
    PhysicalVadConfig,
    PhysicalVadNode,
    SemanticVadNode,
    compile_audio_graph,
)


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


class _Vad:
    def __init__(self, updates: list[VadUpdate], turns: list[bool] | None = None) -> None:
        self.updates = updates
        self.turns = list(turns or [])
        self.postprocessing: VadPostprocessingOptions | None = None

    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
        *,
        postprocessing: VadPostprocessingOptions | None = None,
    ) -> AsyncIterator[VadUpdate]:
        self.postprocessing = postprocessing
        async for _ in audio_chunks:
            pass
        for update in self.updates:
            yield update

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        return self.turns.pop(0)


class _Asr:
    def __init__(self) -> None:
        self.calls: list[AudioSegment] = []

    async def transcribe(
        self,
        segment: AudioSegment,
        options: RecognitionOptions,
    ) -> AsyncIterator[TranscriptionResult]:
        self.calls.append(segment)
        yield TranscriptionResult(
            transcript="结果",
            is_final=True,
            start_sample=segment.start_sample,
            end_sample=segment.end_sample,
            sample_rate=options.sample_rate,
            turn_completed=segment.turn_completed,
            language_code=options.language_code,
        )


async def _chunks() -> AsyncIterator[PcmChunk]:
    yield PcmChunk(pcm_s16le=bytes(16), sample_rate=4, start_sample=0)


@pytest.mark.asyncio
async def test_inferencer_merges_semantic_turn_and_emits_named_outputs(
    assets: dict[str, EngineAssetData],
) -> None:
    physical = _Vad(
        [
            VadUpdate(
                boundaries=(VadBoundary(0, 4), VadBoundary(4, 8)),
                processed_samples=8,
            )
        ]
    )
    semantic = _Vad([], turns=[False, True])
    asr = _Asr()
    inferencer = AudioWorkflowInferencer(
        plan=compile_audio_graph(_definition(), assets=assets),
        engines={"physical": physical, "semantic": semantic, "asr": asr},
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
                recognition=RecognitionOptions(sample_rate=4, language_code="zh-CN"),
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
    assert len(asr.calls) == 1
    assert (asr.calls[0].start_sample, asr.calls[0].end_sample) == (0, 8)


@pytest.mark.asyncio
async def test_inferencer_cancels_all_node_tasks_on_consumer_cancel(
    assets: dict[str, EngineAssetData],
) -> None:
    inferencer = AudioWorkflowInferencer(
        plan=compile_audio_graph(_definition(semantic=False), assets=assets),
        engines={"physical": _Vad([]), "asr": _Asr()},
        runtime_config=AudioRuntimeConfig(1, 1),
        asr_semaphore=asyncio.Semaphore(1),
    )
    stream = inferencer.stream(
        _chunks(),
        AudioInferenceRequest(
            recognition=RecognitionOptions(sample_rate=4, language_code="zh-CN")
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
    inferencer = AudioWorkflowInferencer(
        plan=compile_audio_graph(definition, assets=assets),
        engines={"physical": vad, "asr": _Asr()},
        runtime_config=AudioRuntimeConfig(1, 1),
        asr_semaphore=asyncio.Semaphore(1),
    )

    assert [
        output
        async for output in inferencer.stream(
            _chunks(),
            AudioInferenceRequest(
                recognition=RecognitionOptions(
                    sample_rate=4,
                    language_code="zh-CN",
                )
            ),
        )
    ] == []
    assert vad.postprocessing is not None
    assert vad.postprocessing.processor == "firered.stream_vad"
    assert vad.postprocessing.parameters["speech_threshold"] == 0.7
    assert vad.postprocessing.parameters["min_silence_frame"] == 20
