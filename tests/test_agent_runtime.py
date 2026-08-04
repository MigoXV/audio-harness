from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from audio_harness.assets import (
    EngineApi,
    EngineAssetData,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.domain import SynthesizedAudioChunk
from audio_harness.pipelines.dag import (
    PipelineDefinition,
    compile_pipeline,
    default_node_registry,
)
from audio_harness.pipelines.service import ActivePipelineSnapshot
from audio_harness.serving.realtime.agent import RealtimeAgentRuntime
from audio_harness.serving.realtime.tools import ToolRegistry
from audio_harness.capacity import ResizableConcurrencyLimiter
from audio_harness.workflow_types import WorkflowKind


def _asset(
    asset_id: str,
    *,
    kind: EngineKind,
    api: EngineApi,
    protocol: EngineProtocol,
) -> EngineAssetData:
    now = datetime.now(timezone.utc)
    return EngineAssetData(
        id=asset_id,
        slug=asset_id,
        name=asset_id,
        kind=kind,
        api=api,
        protocol=protocol,
        target="test",
        connect_timeout_seconds=1,
        request_timeout_seconds=None,
        credential_env="TEST_KEY" if kind is EngineKind.LLM else None,
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


def _pipeline(*, thinking_enabled: bool = False) -> ActivePipelineSnapshot:
    definition = PipelineDefinition.model_validate(
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
                {"id": "asr", "type": "asr", "engine_id": "asr-engine"},
                {
                    "id": "agent",
                    "type": "responses_agent",
                    "engine_id": "responses-engine",
                    "config": {"thinking_enabled": thinking_enabled},
                },
                {
                    "id": "tts",
                    "type": "duplex_tts",
                    "engine_id": "tts-engine",
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
                    "id": "vad-asr",
                    "source_node": "vad",
                    "source_port": "segments",
                    "target_node": "asr",
                    "target_port": "segments",
                },
                {
                    "id": "asr-agent",
                    "source_node": "asr",
                    "source_port": "events",
                    "target_node": "agent",
                    "target_port": "events",
                },
                {
                    "id": "agent-tts",
                    "source_node": "agent",
                    "source_port": "events",
                    "target_node": "tts",
                    "target_port": "events",
                },
                {
                    "id": "tts-output",
                    "source_node": "tts",
                    "source_port": "events",
                    "target_node": "output",
                    "target_port": "events",
                },
            ],
        }
    )
    assets = {
        "vad-engine": _asset(
            "vad-engine",
            kind=EngineKind.VAD,
            api=EngineApi.UX_VAD,
            protocol=EngineProtocol.GRPC,
        ),
        "asr-engine": _asset(
            "asr-engine",
            kind=EngineKind.ASR,
            api=EngineApi.UX_SPEECH,
            protocol=EngineProtocol.GRPC,
        ),
        "responses-engine": _asset(
            "responses-engine",
            kind=EngineKind.LLM,
            api=EngineApi.RESPONSES,
            protocol=EngineProtocol.HTTPS,
        ),
        "tts-engine": _asset(
            "tts-engine",
            kind=EngineKind.TTS,
            api=EngineApi.QWEN_TTS_DUPLEX,
            protocol=EngineProtocol.GRPC,
        ),
    }
    return ActivePipelineSnapshot(
        definition=definition,
        plan=compile_pipeline(
            definition,
            registry=default_node_registry(),
            assets=assets,
        ),
        revision=1,
        activated_at=datetime.now(timezone.utc),
        workflow_id="agent",
        kind=WorkflowKind.VOICE_AGENT,
    )


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def stream_response(self, **kwargs):
        self.calls.append(kwargs)

        async def events():
            yield SimpleNamespace(
                type="response.output_text.delta",
                delta="你",
            )
            await asyncio.sleep(0)
            yield SimpleNamespace(
                type="response.output_text.delta",
                delta="好",
            )

        return events()


class FakeTts:
    def __init__(self) -> None:
        self.calls = 0
        self.text: list[str] = []

    async def duplex_synthesize(
        self,
        chunks: AsyncIterator[str],
        **_kwargs,
    ) -> AsyncIterator[SynthesizedAudioChunk]:
        self.calls += 1
        async for text in chunks:
            self.text.append(text)
            yield SynthesizedAudioChunk(
                pcm_s16le=b"\0\0" * 240,
                sample_rate=24_000,
                chunk_index=len(self.text) - 1,
            )


class FailingTts:
    async def duplex_synthesize(
        self,
        _chunks: AsyncIterator[str],
        **_kwargs,
    ) -> AsyncIterator[SynthesizedAudioChunk]:
        raise RuntimeError("tts failed")
        yield  # pragma: no cover


class HangingTts:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def duplex_synthesize(
        self,
        chunks: AsyncIterator[str],
        **_kwargs,
    ) -> AsyncIterator[SynthesizedAudioChunk]:
        async for _text in chunks:
            self.started.set()
            yield SynthesizedAudioChunk(
                pcm_s16le=b"\0\0" * 240,
                sample_rate=24_000,
                chunk_index=0,
            )
        await asyncio.Event().wait()


class FakeRouter:
    def __init__(self) -> None:
        self.responses = FakeResponses()
        self.tts = FakeTts()

    @asynccontextmanager
    async def lease_snapshots(self, _snapshots):
        yield {
            ("responses-engine", 1): self.responses,
            ("tts-engine", 1): self.tts,
        }


@pytest.mark.asyncio
async def test_agent_forwards_each_response_delta_to_one_tts_stream() -> None:
    router = FakeRouter()
    events: list[dict] = []
    audio: list[tuple[bytes, int, int]] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    async def send_audio(pcm: bytes, rate: int, epoch: int) -> None:
        audio.append((pcm, rate, epoch))

    runtime = RealtimeAgentRuntime(
        pipeline=_pipeline(),
        engine_router=router,
        send_event=send_event,
        send_audio=send_audio,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    await runtime.respond("你好", input_item_id="user-item")
    while runtime.active:
        await asyncio.sleep(0)

    assert router.tts.calls == 1
    assert router.tts.text == ["你", "好"]
    assert len(audio) == 2
    assert [
        event["type"]
        for event in events
        if event["type"] == "audio_harness.tts_first_audio"
    ] == ["audio_harness.tts_first_audio"]
    completed = [
        event
        for event in events
        if event["type"] == "response.done"
    ][-1]
    assert completed["response"]["status"] == "completed"
    assert completed["response"]["output"][0]["status"] == "completed"
    assert router.responses.calls[0]["reasoning"] == {"effort": "none"}


@pytest.mark.asyncio
async def test_agent_reports_first_tts_audio_for_wss_once() -> None:
    router = FakeRouter()
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    runtime = RealtimeAgentRuntime(
        pipeline=_pipeline(thinking_enabled=True),
        engine_router=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    await runtime.respond("你好", input_item_id="user-item")
    await asyncio.wait_for(runtime._task, timeout=1)

    assert [
        event["type"]
        for event in events
        if event["type"] == "audio_harness.tts_first_audio"
    ] == ["audio_harness.tts_first_audio"]
    assert len(
        [
            event
            for event in events
            if event["type"] == "response.output_audio.delta"
        ]
    ) == 2
    assert router.responses.calls[0]["reasoning"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_agent_interrupt_advances_epoch_and_clears_audio() -> None:
    router = FakeRouter()
    cleared: list[int] = []

    async def send_event(_event: dict) -> None:
        return None

    async def clear_audio(epoch: int) -> None:
        cleared.append(epoch)

    runtime = RealtimeAgentRuntime(
        pipeline=_pipeline(),
        engine_router=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=clear_audio,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    await runtime.respond("你好", input_item_id="user-item")
    await runtime.interrupt(reason="speech_started")

    assert not runtime.active
    assert cleared and cleared[-1] > 0


@pytest.mark.asyncio
async def test_agent_does_not_deadlock_when_tts_fails_early() -> None:
    router = FakeRouter()
    router.tts = FailingTts()
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    runtime = RealtimeAgentRuntime(
        pipeline=_pipeline(),
        engine_router=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    await runtime.respond("你好", input_item_id="user-item")
    await asyncio.wait_for(runtime._task, timeout=1)

    assert any(event["type"] == "error" for event in events)
    assert events[-1]["response"]["status"] == "failed"


@pytest.mark.asyncio
async def test_agent_interrupt_cancels_hanging_tts_and_releases_slot() -> None:
    router = FakeRouter()
    hanging_tts = HangingTts()
    router.tts = hanging_tts
    limiter = ResizableConcurrencyLimiter(1)

    async def send_event(_event: dict) -> None:
        return None

    runtime = RealtimeAgentRuntime(
        pipeline=_pipeline(),
        engine_router=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=limiter,
        tool_registry=ToolRegistry(),
    )
    await runtime.respond("第一句", input_item_id="user-item-1")
    await asyncio.wait_for(hanging_tts.started.wait(), timeout=1)

    await asyncio.wait_for(runtime.interrupt(), timeout=1)

    assert limiter.active == 0
    assert not runtime.active

    router.tts = FakeTts()
    await runtime.respond("第二句", input_item_id="user-item-2")
    await asyncio.wait_for(runtime._task, timeout=1)

    assert router.tts.calls == 1
    assert limiter.active == 0
