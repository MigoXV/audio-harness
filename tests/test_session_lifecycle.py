from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from audio_harness.configs import WorkflowRuntimeConfig
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.limits import ResizableConcurrencyLimiter
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.sessions import AudioWorkflowSessionService
from audio_harness.sessions.limits import ResizableSessionLimiter
from audio_harness.workflows.settings import WorkflowSettingsSnapshot
from tests.graph_fakes import static_audio_workflow


class _Vad(VadInferencer):
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self._failure = failure

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass

    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
        *,
        postprocessing: VadInferencer.Postprocessing | None = None,
    ) -> AsyncIterator[VadInferencer.Update]:
        del postprocessing, sample_rate
        total = 0
        async for chunk in audio_chunks:
            total += len(chunk) // 2
        if self._failure is not None:
            raise self._failure
        yield VadInferencer.Update(((0, total),), total)

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        return True


class _Asr(AsrInferencer):
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
        del audio
        yield AsrInferencer.Result(
            "ok",
            True,
            start_sample,
            end_sample,
            options.sample_rate,
            turn_completed,
            options.language_code,
        )


class _Settings:
    def __init__(self) -> None:
        self._value = WorkflowSettingsSnapshot(
            config=WorkflowRuntimeConfig(
                min_chunk_bytes=4,
                audio_queue_capacity=2,
                segment_queue_capacity=2,
                result_queue_capacity=4,
            ),
            grpc_audio_workflow_id="default-audio",
            revision=1,
            updated_at=datetime.now(timezone.utc),
        )

    def current(self) -> WorkflowSettingsSnapshot:
        return self._value


def _service(*, asr: object, vad: object):
    pool, workflows, workflow = static_audio_workflow(asr=asr, vad=vad)
    return (
        AudioWorkflowSessionService(
            inferencer_pool=pool,  # type: ignore[arg-type]
            workflows=workflows,  # type: ignore[arg-type]
            workflow_settings=_Settings(),  # type: ignore[arg-type]
            session_limiter=ResizableSessionLimiter(1),
            asr_limiter=ResizableConcurrencyLimiter(1),
        ),
        pool,
        workflow,
    )


def _options() -> AsrInferencer.Options:
    return AsrInferencer.Options(sample_rate=16_000, language_code="zh-CN")


@pytest.mark.asyncio
async def test_reservation_releases_lease_after_normal_end_and_rotation() -> None:
    service, pool, workflow = _service(asr=_Asr(), vad=_Vad())

    async with service.reserve() as reservation:
        first = await reservation.create(options=_options(), workflow=workflow)
        assert pool.active_leases == 1
        await first.append(b"\0\0")
        await first.finish()
        assert [item async for item in first.outputs()]
        await reservation.close_session()
        assert pool.active_leases == 0

        second = await reservation.create(options=_options(), workflow=workflow)
        assert pool.active_leases == 1
        await second.close()
        await reservation.close_session()
        assert pool.active_leases == 0


@pytest.mark.asyncio
async def test_reservation_releases_lease_on_cancel_and_node_failure() -> None:
    service, pool, workflow = _service(
        asr=_Asr(),
        vad=_Vad(failure=RuntimeError("vad failed")),
    )

    async with service.reserve() as reservation:
        session = await reservation.create(options=_options(), workflow=workflow)
        await session.append(b"\0\0")
        await session.finish()
        with pytest.raises(RuntimeError, match="vad failed"):
            _ = [item async for item in session.outputs()]
        await reservation.close_session()
        assert pool.active_leases == 0

        session = await reservation.create(options=_options(), workflow=workflow)
        await session.close()
        await reservation.close_session()
        assert pool.active_leases == 0

    await asyncio.sleep(0)
    assert not any(
        task.get_name().startswith("audio-workflow-session")
        and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_reservation_releases_lease_when_runtime_start_fails() -> None:
    wrong_capability = _Vad()
    service, pool, workflow = _service(asr=wrong_capability, vad=wrong_capability)

    async with service.reserve() as reservation:
        with pytest.raises(TypeError, match="asr.*AsrInferencer"):
            await reservation.create(options=_options(), workflow=workflow)
        assert pool.active_leases == 0
