from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

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
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.media.pcm import PcmChunk
from audio_harness.workflows import (
    AsrNode,
    AsrNodeConfig,
    AudioEdge,
    AudioGraphValidationError,
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
