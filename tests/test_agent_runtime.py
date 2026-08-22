from __future__ import annotations

import asyncio
import base64
import io
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from openai.types.realtime import RealtimeServerEvent
from pydantic import TypeAdapter

from audio_harness.engines.models import (
    EngineApi,
    EngineAssetData,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.inferencers.tts.base import TtsInferencer
from audio_harness.serving.realtime.agent import (
    RealtimeAgentRuntime,
    _FunctionCall,
)
from audio_harness.serving.realtime.tools import McpToolPool, ToolRegistry
from audio_harness.inferencers.limits import ResizableConcurrencyLimiter
from audio_harness.voice_agents.models import (
    DuplexTtsConfig,
    ResponsesConfig,
    SpeakerContextConfig,
    VoiceAgentDefinition,
)
from audio_harness.voice_agents.service import ActiveVoiceAgent
from audio_harness.voice_agents.speaker_context import (
    SpeakerContext,
    speaker_context_policy_instructions,
)
from tests.graph_fakes import static_audio_workflow


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


def _profile(*, thinking_enabled: bool = False) -> ActiveVoiceAgent:
    _pool, _workflows, workflow = static_audio_workflow(
        asr=object(),
        vad=object(),
    )
    responses = _asset(
        "responses-engine",
        kind=EngineKind.LLM,
        api=EngineApi.RESPONSES,
        protocol=EngineProtocol.HTTPS,
    ).snapshot()
    tts = _asset(
        "tts-engine",
        kind=EngineKind.TTS,
        api=EngineApi.QWEN_TTS_DUPLEX,
        protocol=EngineProtocol.GRPC,
    ).snapshot()
    return ActiveVoiceAgent(
        id="agent",
        revision=1,
        definition=VoiceAgentDefinition(
            audio_workflow_id=workflow.id,
            responses_engine_id=responses.id,
            responses=ResponsesConfig(
                model="test-model",
                thinking_enabled=thinking_enabled,
            ),
            tts_engine_id=tts.id,
            tts=DuplexTtsConfig(
                default_voice="test-voice",
                allowed_voices=["test-voice"],
            ),
        ),
        audio_workflow=workflow,
        responses_engine=responses,
        tts_engine=tts,
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


class BlockingResponses(FakeResponses):
    def __init__(self, expected: int = 2) -> None:
        super().__init__()
        self.expected = expected
        self.started_count = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_response(self, **kwargs):
        self.calls.append(kwargs)

        async def events():
            self.started_count += 1
            if self.started_count >= self.expected:
                self.started.set()
            await self.release.wait()
            yield SimpleNamespace(
                type="response.output_text.delta",
                delta="ok",
            )

        return events()


class ClientToolResponses(FakeResponses):
    async def stream_response(self, **kwargs):
        self.calls.append(kwargs)

        async def events():
            yield SimpleNamespace(
                type="response.output_item.done",
                item=SimpleNamespace(
                    type="function_call",
                    id="item_client_tool",
                    call_id="call_client_tool",
                    name="client_tool",
                    arguments="{}",
                ),
            )

        return events()


class ClientToolThenTextResponses(FakeResponses):
    async def stream_response(self, **kwargs):
        self.calls.append(kwargs)
        round_index = len(self.calls)

        async def events():
            if round_index == 1:
                yield SimpleNamespace(
                    type="response.output_item.done",
                    item=SimpleNamespace(
                        type="function_call",
                        id="item_client_tool",
                        call_id="call_client_tool",
                        name="client_tool",
                        arguments="{}",
                    ),
                )
            else:
                yield SimpleNamespace(
                    type="response.output_text.delta",
                    delta="done",
                )

        return events()


class PartialBlockingResponses(FakeResponses):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_response(self, **kwargs):
        self.calls.append(kwargs)

        async def events():
            yield SimpleNamespace(
                type="response.output_text.delta",
                delta="partial",
            )
            self.started.set()
            await self.release.wait()

        return events()


class FakeTts:
    def __init__(self) -> None:
        self.calls = 0
        self.text: list[str] = []

    async def duplex_synthesize(
        self,
        chunks: AsyncIterator[str],
        **_kwargs,
    ) -> AsyncIterator[TtsInferencer.Chunk]:
        self.calls += 1
        async for text in chunks:
            self.text.append(text)
            yield TtsInferencer.Chunk(
                pcm_s16le=b"\0\0" * 240,
                sample_rate=24_000,
                chunk_index=len(self.text) - 1,
            )


class FailingTts:
    async def duplex_synthesize(
        self,
        _chunks: AsyncIterator[str],
        **_kwargs,
    ) -> AsyncIterator[TtsInferencer.Chunk]:
        raise RuntimeError("tts failed")
        yield  # pragma: no cover


class HangingTts:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def duplex_synthesize(
        self,
        chunks: AsyncIterator[str],
        **_kwargs,
    ) -> AsyncIterator[TtsInferencer.Chunk]:
        async for _text in chunks:
            self.started.set()
            yield TtsInferencer.Chunk(
                pcm_s16le=b"\0\0" * 240,
                sample_rate=24_000,
                chunk_index=0,
            )
        await asyncio.Event().wait()


class FakePool:
    def __init__(self) -> None:
        self.responses = FakeResponses()
        self.tts = FakeTts()

    @asynccontextmanager
    async def lease(self, _snapshots):
        yield {
            ("responses-engine", 1): self.responses,
            ("tts-engine", 1): self.tts,
        }


@pytest.mark.asyncio
async def test_agent_forwards_each_response_delta_to_one_tts_stream() -> None:
    router = FakePool()
    events: list[dict] = []
    audio: list[tuple[bytes, int, int]] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    async def send_audio(pcm: bytes, rate: int, epoch: int) -> None:
        audio.append((pcm, rate, epoch))

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
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
    assert all(
        not event["type"].startswith("audio_harness.")
        for event in events
    )
    completed = [
        event
        for event in events
        if event["type"] == "response.done"
    ][-1]
    assert completed["response"]["status"] == "completed"
    assert completed["response"]["output"][0]["status"] == "completed"
    assert router.responses.calls[0]["reasoning"] == {"effort": "none"}


@pytest.mark.asyncio
async def test_agent_appends_trusted_speaker_context_after_user_instructions() -> None:
    router = FakePool()
    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
        send_event=lambda _event: asyncio.sleep(0),
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    runtime.update_options({"instructions": "用户自定义指令"})

    await runtime.respond(
        "你好",
        input_item_id="user-item",
        speaker_context=SpeakerContext("speaker-001", 0.91),
    )
    while runtime.active:
        await asyncio.sleep(0)

    instructions = router.responses.calls[0]["instructions"]
    assert instructions.index("用户自定义指令") < instructions.index(
        "[Audio Harness 可信说话人上下文]"
    )
    assert "speaker_id=speaker-001" in instructions
    assert "score=0.910000" in instructions
    assert "账户级身份认证" in instructions
    assert "payload" not in instructions
    assert "speaker-002" not in instructions


@pytest.mark.asyncio
async def test_agent_applies_published_speaker_policy_only_when_turn_matches() -> None:
    router = FakePool()
    base = _profile()
    config = SpeakerContextConfig(
        enabled=True,
        speaker_prompt="speaker-001 使用简洁中文回复。",
    )
    runtime = RealtimeAgentRuntime(
        profile=replace(
            base,
            definition=base.definition.model_copy(
                update={
                    "responses": base.definition.responses.model_copy(
                        update={"instructions": "主系统提示词"}
                    ),
                    "speaker_context": config,
                }
            ),
            speaker_context_policy=speaker_context_policy_instructions(config),
        ),
        inferencer_pool=router,
        send_event=lambda _event: asyncio.sleep(0),
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )

    await runtime.respond(
        "你好",
        input_item_id="matched",
        speaker_context=SpeakerContext("speaker-001", 0.91),
    )
    while runtime.active:
        await asyncio.sleep(0)
    matched_instructions = router.responses.calls[0]["instructions"]
    assert "speaker-001 使用简洁中文回复。" in matched_instructions
    assert matched_instructions.index("主系统提示词") < matched_instructions.index(
        "speaker-001 使用简洁中文回复。"
    )

    await runtime.respond("再见", input_item_id="unmatched")
    while runtime.active:
        await asyncio.sleep(0)
    unmatched_instructions = router.responses.calls[1]["instructions"]
    assert "主系统提示词" in unmatched_instructions
    assert "speaker-001 使用简洁中文回复。" not in unmatched_instructions


@pytest.mark.asyncio
async def test_agent_reports_wss_audio_with_official_events_only() -> None:
    router = FakePool()
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    runtime = RealtimeAgentRuntime(
        profile=_profile(thinking_enabled=True),
        inferencer_pool=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    await runtime.respond("你好", input_item_id="user-item")
    await asyncio.wait_for(runtime._task, timeout=1)

    assert all(
        not event["type"].startswith("audio_harness.")
        for event in events
    )
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
    router = FakePool()
    cleared: list[int] = []
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    async def clear_audio(epoch: int) -> None:
        cleared.append(epoch)

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=clear_audio,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    await runtime.respond("你好", input_item_id="user-item")
    await runtime.interrupt(reason="turn_detected")

    assert not runtime.active
    assert cleared and cleared[-1] > 0
    assert events[-1]["type"] == "output_audio_buffer.cleared"
    done = next(
        event for event in events if event["type"] == "response.done"
    )
    assert done["response"]["status_details"] == {
        "type": "cancelled",
        "reason": "turn_detected",
    }
    adapter = TypeAdapter(RealtimeServerEvent)
    assert all(adapter.validate_python(event) for event in events)


@pytest.mark.asyncio
async def test_agent_synchronizes_webrtc_track_epoch_before_audio() -> None:
    router = FakePool()
    cleared: list[int] = []
    audio_epochs: list[int] = []

    async def send_event(_event: dict) -> None:
        return None

    async def send_audio(_pcm: bytes, _rate: int, epoch: int) -> None:
        audio_epochs.append(epoch)

    async def clear_audio(epoch: int) -> None:
        cleared.append(epoch)

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
        send_event=send_event,
        send_audio=send_audio,
        clear_audio=clear_audio,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    await runtime.respond("你好", input_item_id="user-item")
    task = runtime._task
    assert task is not None
    await task

    assert audio_epochs
    assert cleared[0] == audio_epochs[0]


@pytest.mark.asyncio
async def test_agent_does_not_deadlock_when_tts_fails_early() -> None:
    router = FakePool()
    router.tts = FailingTts()
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
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
    router = FakePool()
    hanging_tts = HangingTts()
    router.tts = hanging_tts
    limiter = ResizableConcurrencyLimiter(1)

    async def send_event(_event: dict) -> None:
        return None

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
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


@pytest.mark.asyncio
async def test_agent_text_response_matches_live_official_event_order() -> None:
    router = FakePool()
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    runtime.create_conversation_item(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
    )
    await runtime.create_response({"output_modalities": ["text"]})
    task = runtime._task
    assert task is not None
    await task

    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_item.added",
        "conversation.item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "conversation.item.done",
        "response.output_item.done",
        "response.done",
    ]
    adapter = TypeAdapter(RealtimeServerEvent)
    assert all(adapter.validate_python(event) for event in events)


@pytest.mark.asyncio
async def test_agent_response_create_applies_per_response_overrides() -> None:
    router = FakePool()

    async def send_event(_event: dict) -> None:
        return None

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    await runtime.create_response(
        {
            "conversation": "none",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
            "instructions": "only this response",
            "max_output_tokens": 7,
            "output_modalities": ["text"],
            "parallel_tool_calls": False,
            "reasoning": {"effort": "low"},
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "lookup",
                    "parameters": {"type": "object"},
                }
            ],
        }
    )
    task = runtime._task
    assert task is not None
    await task

    call = router.responses.calls[0]
    assert call["max_output_tokens"] == 7
    assert call["reasoning"] == {"effort": "low"}
    assert call["parallel_tool_calls"] is False
    assert call["instructions"].endswith("only this response")
    assert call["input"][0]["content"][0]["text"] == "hello"


def test_rejected_session_update_is_atomic() -> None:
    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=FakePool(),
        send_event=lambda _event: asyncio.sleep(0),
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    before = runtime.session_document(model="voice-agent")

    with pytest.raises(ValueError, match="工作流不允许音色"):
        runtime.update_options(
            {
                "output_modalities": ["text"],
                "audio": {"output": {"voice": "not-allowed"}},
            }
        )

    assert runtime.session_document(model="voice-agent") == before


@pytest.mark.asyncio
async def test_immediate_cancel_still_follows_response_created() -> None:
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=FakePool(),
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    response_id = await runtime.create_response(
        {
            "conversation": "none",
            "input": [],
            "output_modalities": ["text"],
        }
    )

    await runtime.interrupt(
        response_id=response_id,
        reason="client_cancelled",
    )

    assert [event["type"] for event in events] == [
        "response.created",
        "response.done",
    ]
    assert events[-1]["response"]["status"] == "cancelled"
    adapter = TypeAdapter(RealtimeServerEvent)
    assert all(adapter.validate_python(event) for event in events)


@pytest.mark.asyncio
async def test_response_done_collects_function_call_items() -> None:
    router = FakePool()
    router.responses = ClientToolThenTextResponses()
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    await runtime.create_response(
        {
            "conversation": "none",
            "input": [],
            "output_modalities": ["text"],
            "tools": [
                {
                    "type": "function",
                    "name": "client_tool",
                    "description": "client tool",
                    "parameters": {"type": "object"},
                }
            ],
        }
    )

    async def wait_for_pending_tool() -> None:
        while not runtime._pending_client_tools:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_pending_tool(), timeout=1)
    assert await runtime.submit_function_output(
        "call_client_tool",
        "ok",
    ) is False
    task = runtime._task
    assert task is not None
    await asyncio.wait_for(task, timeout=1)

    done = next(
        event for event in reversed(events) if event["type"] == "response.done"
    )
    assert [item["type"] for item in done["response"]["output"]] == [
        "message",
        "function_call",
    ]
    adapter = TypeAdapter(RealtimeServerEvent)
    assert all(adapter.validate_python(event) for event in events)


@pytest.mark.asyncio
async def test_oob_response_context_does_not_mutate_default_conversation() -> None:
    router = FakePool()

    async def send_event(_event: dict) -> None:
        return None

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    runtime.create_conversation_item(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "saved"}],
        }
    )
    await runtime.create_response(
        {
            "conversation": "none",
            "output_modalities": ["text"],
        }
    )
    task = runtime._task
    assert task is not None
    await task
    assert router.responses.calls[-1]["input"][0]["content"][0]["text"] == (
        "saved"
    )

    await runtime.create_response(
        {
            "conversation": "none",
            "input": [],
            "output_modalities": ["text"],
        }
    )
    task = runtime._task
    assert task is not None
    await task

    assert router.responses.calls[-1]["input"] == []
    assert runtime._history[0]["content"][0]["text"] == "saved"


@pytest.mark.parametrize(
    "audio",
    [
        {"input": {"format": {"type": "audio/pcmu"}}},
        {"output": {"format": {"type": "audio/pcma"}}},
    ],
)
def test_agent_rejects_g711_audio_formats(audio: dict) -> None:
    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=FakePool(),
        send_event=lambda _event: asyncio.sleep(0),
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )

    with pytest.raises(ValueError, match="不支持的音频格式"):
        runtime.update_options({"audio": audio})


def test_agent_accepts_16_khz_input_but_keeps_output_at_24_khz() -> None:
    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=FakePool(),
        send_event=lambda _event: asyncio.sleep(0),
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )

    runtime.update_options(
        {
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 16_000}
                }
            }
        }
    )

    assert runtime.options.input_audio_format == {
        "type": "audio/pcm",
        "rate": 16_000,
    }
    assert runtime.options.output_audio_format == {
        "type": "audio/pcm",
        "rate": 24_000,
    }
    with pytest.raises(ValueError, match="输出只支持 24 kHz"):
        runtime.update_options(
            {
                "audio": {
                    "output": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": 16_000,
                        }
                    }
                }
            }
        )


def test_agent_rejects_incomplete_pcm_sample() -> None:
    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=FakePool(),
        send_event=lambda _event: asyncio.sleep(0),
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )

    with pytest.raises(ValueError, match="PCM S16LE"):
        runtime.create_conversation_item(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "audio": base64.b64encode(b"\0").decode("ascii"),
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_agent_runs_concurrent_oob_text_responses_and_cancels_by_id() -> None:
    router = FakePool()
    blocking = BlockingResponses()
    router.responses = blocking
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    request = {
        "conversation": "none",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            }
        ],
        "output_modalities": ["text"],
    }
    first = await runtime.create_response(request)
    second = await runtime.create_response(request)
    second_task = runtime._response_tasks[second]
    await asyncio.wait_for(blocking.started.wait(), timeout=1)

    await runtime.interrupt(response_id=first, reason="client_cancelled")
    assert second in runtime._response_tasks
    blocking.release.set()
    await asyncio.wait_for(second_task, timeout=1)

    statuses = {
        event["response"]["id"]: event["response"]["status"]
        for event in events
        if event["type"] == "response.done"
    }
    assert statuses[first] == "cancelled"
    assert statuses[second] == "completed"
    adapter = TypeAdapter(RealtimeServerEvent)
    assert all(adapter.validate_python(event) for event in events)


@pytest.mark.asyncio
async def test_concurrent_cancel_emits_one_terminal_event() -> None:
    router = FakePool()
    blocking = BlockingResponses(expected=1)
    router.responses = blocking
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    response_id = await runtime.create_response(
        {
            "conversation": "none",
            "input": [],
            "output_modalities": ["text"],
        }
    )
    await asyncio.wait_for(blocking.started.wait(), timeout=1)

    results = await asyncio.gather(
        runtime.interrupt(
            response_id=response_id,
            reason="client_cancelled",
        ),
        runtime.interrupt(
            response_id=response_id,
            reason="client_cancelled",
        ),
        return_exceptions=True,
    )

    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert sum(
        event["type"] == "response.done"
        and event["response"]["status"] == "cancelled"
        for event in events
    ) == 1


@pytest.mark.asyncio
async def test_cancel_finalizes_partial_output_as_incomplete() -> None:
    router = FakePool()
    partial = PartialBlockingResponses()
    router.responses = partial
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    response_id = await runtime.create_response(
        {
            "conversation": "none",
            "input": [],
            "output_modalities": ["text"],
        }
    )
    await asyncio.wait_for(partial.started.wait(), timeout=1)

    await runtime.interrupt(
        response_id=response_id,
        reason="client_cancelled",
    )

    terminal_types = [event["type"] for event in events[-5:]]
    assert terminal_types == [
        "response.output_text.done",
        "response.content_part.done",
        "conversation.item.done",
        "response.output_item.done",
        "response.done",
    ]
    output_item = events[-2]["item"]
    done_response = events[-1]["response"]
    assert output_item["status"] == "incomplete"
    assert output_item["content"][0]["text"] == "partial"
    assert done_response["status"] == "cancelled"
    assert done_response["output"] == [output_item]
    adapter = TypeAdapter(RealtimeServerEvent)
    assert all(adapter.validate_python(event) for event in events)


@pytest.mark.asyncio
async def test_internal_cancel_missing_response_is_idempotent() -> None:
    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=FakePool(),
        send_event=lambda _event: asyncio.sleep(0),
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )

    await runtime.interrupt(response_id="missing", emit=False)
    await runtime.close()


@pytest.mark.asyncio
async def test_concurrent_audio_response_create_allows_only_one() -> None:
    clear_started = asyncio.Event()
    release_clear = asyncio.Event()

    async def clear_audio(_epoch: int) -> None:
        clear_started.set()
        await release_clear.wait()

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=FakePool(),
        send_event=lambda _event: asyncio.sleep(0),
        send_audio=lambda _pcm, _rate, _epoch: asyncio.sleep(0),
        clear_audio=clear_audio,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    request = {
        "conversation": "none",
        "input": [],
        "output_modalities": ["audio"],
    }
    first = asyncio.create_task(runtime.create_response(request))
    await asyncio.wait_for(clear_started.wait(), timeout=1)

    with pytest.raises(ValueError, match="一次只能生成一个音频响应"):
        await runtime.create_response(request)

    release_clear.set()
    await first
    await runtime.close()


@pytest.mark.asyncio
async def test_client_tool_wait_is_removed_when_response_is_cancelled() -> None:
    router = FakePool()
    router.responses = ClientToolResponses()
    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=router,
        send_event=lambda _event: asyncio.sleep(0),
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )
    response_id = await runtime.create_response(
        {
            "conversation": "none",
            "input": [],
            "output_modalities": ["text"],
            "tools": [
                {
                    "type": "function",
                    "name": "client_tool",
                    "description": "client tool",
                    "parameters": {"type": "object"},
                }
            ],
        }
    )
    async def wait_for_pending_tool() -> None:
        while not runtime._pending_client_tools:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_pending_tool(), timeout=1)

    await runtime.interrupt(
        response_id=response_id,
        reason="client_cancelled",
    )

    assert runtime._pending_client_tools == {}


@pytest.mark.asyncio
async def test_tool_events_follow_response_conversation_boundary() -> None:
    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=FakePool(),
        send_event=send_event,
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=ToolRegistry(),
    )

    await runtime._emit_tool_output(
        "call_oob",
        "oob",
        add_to_conversation=False,
    )
    assert events == []
    assert runtime._history == []

    call = _FunctionCall(
        call_id="call_default",
        name="lookup",
        arguments="{}",
        item_id="item_default_call",
        output_index=1,
    )
    await runtime._emit_function_call(
        call,
        response_id="response_default",
        add_to_conversation=True,
    )
    await runtime._emit_tool_output(
        call.call_id,
        "ok",
        add_to_conversation=True,
    )

    assert [item["type"] for item in runtime._history] == [
        "function_call",
        "function_call_output",
    ]
    assert runtime.retrieve_conversation_item(call.item_id)["call_id"] == (
        call.call_id
    )


@pytest.mark.asyncio
async def test_parallel_tool_failure_cancels_siblings() -> None:
    registry = ToolRegistry()
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()

    async def slow(_arguments: dict) -> str:
        slow_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            slow_cancelled.set()

    async def fail(_arguments: dict) -> str:
        await slow_started.wait()
        raise RuntimeError("boom")

    for name, handler in (("slow", slow), ("fail", fail)):
        registry.register(
            name=name,
            description=name,
            parameters={"type": "object"},
            handler=handler,
        )
    runtime = RealtimeAgentRuntime(
        profile=_profile(),
        inferencer_pool=FakePool(),
        send_event=lambda _event: asyncio.sleep(0),
        send_audio=None,
        clear_audio=None,
        responses_limiter=None,
        tts_limiter=None,
        tool_registry=registry,
    )
    calls = [
        _FunctionCall(
            call_id=f"call_{name}",
            name=name,
            arguments="{}",
            item_id=f"item_{name}",
            output_index=index,
        )
        for index, name in enumerate(("slow", "fail"))
    ]

    with pytest.raises(RuntimeError, match="boom"):
        await runtime._execute_calls(
            calls,
            client_tool_names=set(),
            mcp=McpToolPool([]),
            history=[],
            add_to_conversation=True,
        )

    assert slow_cancelled.is_set()
