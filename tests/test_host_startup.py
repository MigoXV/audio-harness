from __future__ import annotations

from types import SimpleNamespace

import pytest

from audio_harness.serving.host import AudioHarnessHost


class AsyncRecorder:
    def __init__(self, events: list[str], name: str, result=None) -> None:
        self._events = events
        self._name = name
        self._result = result

    async def __call__(self, *_args, **_kwargs):
        self._events.append(self._name)
        return self._result


@pytest.mark.asyncio
async def test_host_start_does_not_prepare_inference_engines() -> None:
    events: list[str] = []
    host = object.__new__(AudioHarnessHost)
    host.database = SimpleNamespace(
        ping=AsyncRecorder(events, "database.ping"),
        close=AsyncRecorder(events, "database.close"),
    )
    host.workflow_settings = SimpleNamespace(
        initialize=AsyncRecorder(events, "workflow_settings.initialize"),
    )
    host.capacity_settings = SimpleNamespace(
        initialize=AsyncRecorder(
            events,
            "capacity_settings.initialize",
            result="capacity",
        ),
    )
    host.capacity = SimpleNamespace(
        apply=AsyncRecorder(events, "capacity.apply"),
    )
    host.pipeline_store = SimpleNamespace(
        initialize=AsyncRecorder(events, "pipeline_store.initialize"),
    )
    host.grpc_server = SimpleNamespace(
        start=AsyncRecorder(events, "grpc_server.start"),
    )

    async def reject_prepare(*_args, **_kwargs) -> None:
        raise AssertionError("启动期间不应准备或探测推理引擎")

    host.engine_router = SimpleNamespace(
        prepare_all=reject_prepare,
        close=AsyncRecorder(events, "engine_router.close"),
    )
    host._started = False
    host._closed = False

    await host.start()

    assert host._started is True
    assert events == [
        "database.ping",
        "workflow_settings.initialize",
        "capacity_settings.initialize",
        "capacity.apply",
        "pipeline_store.initialize",
        "grpc_server.start",
    ]
