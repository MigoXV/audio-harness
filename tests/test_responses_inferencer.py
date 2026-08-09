from __future__ import annotations

from types import SimpleNamespace

import pytest

from audio_harness.assets import EngineApi, EngineProtocol, EngineSnapshot
from audio_harness.inferencers.http.responses import ResponsesInferencer


@pytest.mark.asyncio
async def test_responses_models_are_normalized_and_sorted(monkeypatch) -> None:
    inferencer = ResponsesInferencer(
        EngineSnapshot(
            id="responses-1",
            revision=1,
            name="Responses",
            api=EngineApi.RESPONSES,
            protocol=EngineProtocol.HTTPS,
            target="https://example.test/v1",
            connect_timeout_seconds=1,
            request_timeout_seconds=10,
            api_key="test-key",
        )
    )

    async def list_models():
        return SimpleNamespace(
            data=[
                SimpleNamespace(id=" model-z "),
                SimpleNamespace(id="model-a"),
                SimpleNamespace(id="model-z"),
                SimpleNamespace(id=" "),
            ]
        )

    monkeypatch.setattr(inferencer.client.models, "list", list_models)
    try:
        assert await inferencer.list_models() == ("model-a", "model-z")
    finally:
        await inferencer.close()
