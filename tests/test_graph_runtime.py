from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from audio_harness.assets import (
    EngineApi,
    EngineAssetData,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.domain import (
    AudioSegment,
    RecognitionOptions,
    TranscriptionResult,
    VadBoundary,
    VadUpdate,
)
from audio_harness.pipelines.dag import (
    PipelineDefinition,
    compile_pipeline,
    default_node_registry,
)
from audio_harness.pipelines.graph_runtime import (
    GraphRuntimeConfig,
    GraphSessionRunner,
)


class FakeVad:
    def __init__(
        self,
        updates: list[VadUpdate],
        *,
        turn_results: list[bool] | None = None,
        turn_error: Exception | None = None,
    ) -> None:
        self.updates = updates
        self.turn_results = list(turn_results or [])
        self.turn_error = turn_error
        self.received = bytearray()
        self.turn_calls: list[bytes] = []

    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadUpdate]:
        async for chunk in audio_chunks:
            self.received.extend(chunk)
        for update in self.updates:
            yield update

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        self.turn_calls.append(audio)
        if self.turn_error is not None:
            raise self.turn_error
        return self.turn_results.pop(0)


class FakeAsr:
    def __init__(self) -> None:
        self.calls: list[AudioSegment] = []

    async def transcribe(
        self,
        segment: AudioSegment,
        options: RecognitionOptions,
    ) -> AsyncIterator[TranscriptionResult]:
        self.calls.append(segment)
        for transcript, is_final in (("临时", False), ("最终", True)):
            yield TranscriptionResult(
                transcript=transcript,
                is_final=is_final,
                start_sample=segment.start_sample,
                end_sample=segment.end_sample,
                sample_rate=options.sample_rate,
                turn_completed=segment.turn_completed,
                language_code=options.language_code,
            )


def engine_asset(
    asset_id: str,
    *,
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


def graph_definition() -> PipelineDefinition:
    return PipelineDefinition.model_validate(
        {
            "nodes": [
                {"id": "input", "type": "audio_input"},
                {
                    "id": "vad",
                    "type": "physical_vad",
                    "engine_id": "vad-engine",
                    "config": {
                        "min_segment_seconds": 0,
                        "max_segment_seconds": 60,
                        "audio_buffer_seconds": 61,
                        "speech_pre_roll_seconds": 0,
                    },
                },
                {
                    "id": "semantic",
                    "type": "semantic_vad",
                    "engine_id": "vad-engine",
                    "failure_policy": "fail_open",
                },
                {
                    "id": "asr",
                    "type": "asr",
                    "engine_id": "asr-engine",
                },
                {"id": "output", "type": "session_output"},
            ],
            "edges": [
                {
                    "id": "input-vad",
                    "source_node": "input",
                    "source_port": "audio",
                    "target_node": "vad",
                    "target_port": "audio",
                },
                {
                    "id": "vad-semantic",
                    "source_node": "vad",
                    "source_port": "segments",
                    "target_node": "semantic",
                    "target_port": "segments",
                },
                {
                    "id": "semantic-asr",
                    "source_node": "semantic",
                    "source_port": "segments",
                    "target_node": "asr",
                    "target_port": "segments",
                },
                {
                    "id": "vad-output",
                    "source_node": "vad",
                    "source_port": "events",
                    "target_node": "output",
                    "target_port": "events",
                },
                {
                    "id": "semantic-output",
                    "source_node": "semantic",
                    "source_port": "events",
                    "target_node": "output",
                    "target_port": "events",
                },
                {
                    "id": "asr-output",
                    "source_node": "asr",
                    "source_port": "events",
                    "target_node": "output",
                    "target_port": "events",
                },
            ],
        }
    )


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def runner(
    *,
    physical: FakeVad,
    semantic: FakeVad,
    asr: FakeAsr,
) -> GraphSessionRunner:
    assets = {
        "vad-engine": engine_asset(
            "vad-engine",
            kind=EngineKind.VAD,
            api=EngineApi.UX_VAD,
        ),
        "asr-engine": engine_asset(
            "asr-engine",
            kind=EngineKind.ASR,
            api=EngineApi.UX_SPEECH,
        ),
    }
    plan = compile_pipeline(
        graph_definition(),
        registry=default_node_registry(),
        assets=assets,
    )
    return GraphSessionRunner(
        plan=plan,
        engines={
            "vad": physical,
            "semantic": semantic,
            "asr": asr,
        },
        options=RecognitionOptions(sample_rate=4, language_code="zh-CN"),
        runtime_config=GraphRuntimeConfig(
            segment_queue_capacity=1,
            result_queue_capacity=1,
        ),
        asr_semaphore=asyncio.Semaphore(1),
        include_vad_events=False,
        include_vad_progress=False,
    )


@pytest.mark.asyncio
async def test_graph_runner_preserves_semantic_turn_merging() -> None:
    physical = FakeVad(
        [
            VadUpdate(
                boundaries=(VadBoundary(0, 4), VadBoundary(4, 8)),
                processed_samples=8,
            )
        ]
    )
    semantic = FakeVad([], turn_results=[False, True])
    asr = FakeAsr()

    results = [
        item
        async for item in runner(
            physical=physical,
            semantic=semantic,
            asr=asr,
        ).run(chunks(bytes(16)))
    ]

    transcriptions = [
        item for item in results if isinstance(item, TranscriptionResult)
    ]
    assert [
        (item.transcript, item.is_final, item.turn_completed)
        for item in transcriptions
    ] == [
        ("", True, False),
        ("临时", False, True),
        ("最终", True, True),
    ]
    assert len(asr.calls) == 1
    assert (asr.calls[0].start_sample, asr.calls[0].end_sample) == (0, 8)


@pytest.mark.asyncio
async def test_graph_runner_applies_semantic_fail_open() -> None:
    physical = FakeVad(
        [
            VadUpdate(
                boundaries=(VadBoundary(0, 4),),
                processed_samples=4,
            )
        ]
    )
    semantic = FakeVad([], turn_error=RuntimeError("unavailable"))
    asr = FakeAsr()

    results = [
        item
        async for item in runner(
            physical=physical,
            semantic=semantic,
            asr=asr,
        ).run(chunks(bytes(8)))
    ]

    assert [
        item.transcript
        for item in results
        if isinstance(item, TranscriptionResult)
    ] == ["临时", "最终"]
    assert len(asr.calls) == 1


@pytest.mark.asyncio
async def test_graph_runner_cleans_up_after_consumer_cancel() -> None:
    physical = FakeVad([], turn_results=[])
    semantic = FakeVad([])
    asr = FakeAsr()
    graph = runner(physical=physical, semantic=semantic, asr=asr)

    stream = graph.run(chunks(bytes(8)))
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await stream.aclose()

    await asyncio.sleep(0)
    assert not any(
        task.get_name().startswith("graph-node-")
        and not task.done()
        for task in asyncio.all_tasks()
    )
