from __future__ import annotations

import asyncio

import pytest

from audio_harness.configs import CapacityConfig, ProtocolCapacityConfig
from audio_harness.errors import CapacityExceededError
from audio_harness.inferencers.limits import ResizableConcurrencyLimiter
from audio_harness.serving.capacity import ServingCapacity
from audio_harness.serving.capacity_settings import CapacitySettingsSnapshot
from audio_harness.sessions.limits import ResizableSessionLimiter
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_lowered_session_limit_keeps_active_session() -> None:
    limiter = ResizableSessionLimiter(2)

    async with limiter.slot():
        await limiter.resize(1)
        assert limiter.active == 1
        with pytest.raises(CapacityExceededError):
            async with limiter.slot():
                pass

    async with limiter.slot():
        assert limiter.active == 1


@pytest.mark.asyncio
async def test_lowered_concurrency_waits_without_cancelling_active_work() -> None:
    limiter = ResizableConcurrencyLimiter(2)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first_job() -> None:
        async with limiter:
            first_started.set()
            await release_first.wait()

    async def second_job() -> None:
        async with limiter:
            second_started.set()

    first = asyncio.create_task(first_job())
    await first_started.wait()
    await limiter.resize(1)
    second = asyncio.create_task(second_job())
    await asyncio.sleep(0)
    assert not second_started.is_set()
    assert not first.cancelled()

    release_first.set()
    await asyncio.gather(first, second)
    assert second_started.is_set()


@pytest.mark.asyncio
async def test_protocol_sessions_are_independent_and_asr_is_shared() -> None:
    capacity = ServingCapacity(
        CapacityConfig(
            grpc=ProtocolCapacityConfig(max_sessions=1),
            realtime=ProtocolCapacityConfig(max_sessions=2),
            webrtc=ProtocolCapacityConfig(max_sessions=3),
            max_asr_concurrency=1,
        )
    )

    async with capacity.grpc_sessions.slot():
        with pytest.raises(CapacityExceededError):
            async with capacity.grpc_sessions.slot():
                pass
        async with capacity.realtime_sessions.slot():
            assert capacity.grpc_sessions.active == 1
            assert capacity.realtime_sessions.active == 1
            async with capacity.webrtc_sessions.slot():
                assert capacity.webrtc_sessions.active == 1

    snapshot = CapacitySettingsSnapshot(
        config=CapacityConfig(
            grpc=ProtocolCapacityConfig(max_sessions=3),
            realtime=ProtocolCapacityConfig(max_sessions=4),
            webrtc=ProtocolCapacityConfig(max_sessions=5),
            max_asr_concurrency=2,
        ),
        revision=2,
        updated_at=datetime.now(timezone.utc),
    )
    await capacity.apply(snapshot)
    assert capacity.grpc_sessions.capacity == 3
    assert capacity.realtime_sessions.capacity == 4
    assert capacity.webrtc_sessions.capacity == 5
    assert capacity.asr.capacity == 2
