"""从已安装 wheel 经真实 WS 入口验证 SDK，不修改服务端执行路径。"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn
from aha import AsyncAudioHarness, AudioFormat, AudioHarness, Limits
from fastapi import FastAPI

from audio_harness.api.routers.data_plane import create_data_plane_router
from tests.test_aha_grpc import make_service


@asynccontextmanager
async def running_ws(service):
    app = FastAPI()
    app.include_router(
        create_data_plane_router(
            realtime=service, webrtc=None, max_message_bytes=1024 * 1024
        )
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off", ws="websockets-sansio")
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))

    async def ready():
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("WS server stopped before startup")
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(ready(), 5)
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        finally:
            listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("sample_rate", [16000, 24000])
async def test_installed_sdk_transcribes_and_releases_existing_service(
    asynchronous, sample_rate
):
    service, pool, capacity = make_service()
    async with running_ws(service) as url:
        settings = dict(
            model="gpt-4o-transcribe",
            audio=AudioFormat.pcm16(sample_rate),
            session={"audio": {"input": {"turn_detection": None}}},
        )
        pcm = b"\0\0" * (sample_rate // 10 + 1)

        def sync_client():
            with AudioHarness(base_url=url, proxy=None) as client:
                with client.transcriptions.connect(**settings) as session:
                    session.send_audio([pcm[:3], pcm[3:]])
                    session.input_audio_buffer.commit()
                    for event in session.events():
                        assert event.type != "error", event
                        if (
                            event.type
                            == "conversation.item.input_audio_transcription.completed"
                        ):
                            return event.transcript

        async def async_client():
            async with AsyncAudioHarness(base_url=url, proxy=None) as client:
                async with client.transcriptions.connect(**settings) as session:
                    await session.send_audio([pcm[:3], pcm[3:]])
                    await session.input_audio_buffer.commit()
                    async for event in session.events():
                        assert event.type != "error", event
                        if (
                            event.type
                            == "conversation.item.input_audio_transcription.completed"
                        ):
                            return event.transcript

        result = await asyncio.wait_for(
            async_client() if asynchronous else asyncio.to_thread(sync_client), 10
        )
        assert result == "你好，世界"
    assert pool.active_leases == 0
    assert capacity.realtime_sessions.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_sdk_agent_text_and_tts_use_existing_runtime(asynchronous):
    from tests.test_agent_runtime import FakePool, _profile

    service, _, _ = make_service()

    class Models:
        async def resolve(self, model):
            assert model == "voice-agent"
            return _profile()

    service.models = Models()
    pool = FakePool()
    service.inferencer_pool = pool
    response = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "你好"}],
            }
        ]
    }

    async with running_ws(service) as url:

        def sync_client():
            with AudioHarness(base_url=url, proxy=None) as client:
                with client.realtime.connect(model="voice-agent") as session:
                    session.response.create(response)
                    events = []
                    for event in session.events():
                        events.append(event)
                        if event.type == "response.done":
                            return events

        async def async_client():
            async with AsyncAudioHarness(base_url=url, proxy=None) as client:
                async with client.realtime.connect(model="voice-agent") as session:
                    await session.response.create(response)
                    events = []
                    async for event in session.events():
                        events.append(event)
                        if event.type == "response.done":
                            return events

        events = await asyncio.wait_for(
            async_client() if asynchronous else asyncio.to_thread(sync_client), 10
        )
        assert not [event for event in events if event.type == "error"]
        chunks = [
            event.delta
            for event in events
            if event.type == "response.output_audio.delta"
        ]
        assert chunks and all(isinstance(chunk, bytes) for chunk in chunks)
        assert events[-1].response.status == "completed"
        assert pool.tts.calls == 1


@pytest.mark.asyncio
async def test_sdk_keeps_recoverable_server_errors_visible():
    service, pool, _ = make_service()
    async with running_ws(service) as url:
        async with AsyncAudioHarness(
            base_url=url, proxy=None, limits=Limits(connect_timeout=2)
        ) as client:
            async with client.transcriptions.connect(
                model="gpt-4o-transcribe"
            ) as session:
                await session.input_audio_buffer.commit()

                async def error_event():
                    async for event in session.events():
                        if event.type == "error":
                            return event

                event = await asyncio.wait_for(error_event(), 5)
                assert event.error.code == "input_audio_buffer_commit_empty"
                # A business rejection leaves the same connection usable.
                await session.input_audio_buffer.clear()
    assert pool.active_leases == 0
