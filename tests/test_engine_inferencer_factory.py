from __future__ import annotations

import pytest

from audio_harness.engines.inferencer_factory import create_inferencer
from audio_harness.engines.models import EngineApi, EngineProtocol, EngineSnapshot
from audio_harness.errors import EngineConfigurationInvalidError
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.asr.grpc import UxSpeechAsrInferencer
from audio_harness.inferencers.responses.http import ResponsesInferencer
from audio_harness.inferencers.transports.grpc import GrpcTransport
from audio_harness.inferencers.tts.base import TtsInferencer
from audio_harness.inferencers.tts.grpc import QwenDuplexTtsInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.inferencers.vad.grpc import UxVadInferencer


def snapshot(api: EngineApi, protocol: EngineProtocol) -> EngineSnapshot:
    return EngineSnapshot(
        id=f"{protocol.value}-{api.value}",
        revision=1,
        name=api.value,
        api=api,
        protocol=protocol,
        target="https://example.test/v1",
        connect_timeout_seconds=1,
        request_timeout_seconds=10,
        api_key="test-key",
    )


@pytest.mark.parametrize(
    ("api", "protocol", "expected_type"),
    [
        (EngineApi.UX_SPEECH, EngineProtocol.GRPC, UxSpeechAsrInferencer),
        (EngineApi.UX_VAD, EngineProtocol.GRPC, UxVadInferencer),
        (
            EngineApi.QWEN_TTS_DUPLEX,
            EngineProtocol.GRPC,
            QwenDuplexTtsInferencer,
        ),
        (EngineApi.RESPONSES, EngineProtocol.HTTPS, ResponsesInferencer),
    ],
)
@pytest.mark.asyncio
async def test_create_inferencer_routes_supported_api_protocol_pairs(
    api: EngineApi,
    protocol: EngineProtocol,
    expected_type: type,
) -> None:
    inferencer = create_inferencer(
        snapshot(api, protocol),
        max_message_bytes=4096,
    )

    try:
        assert isinstance(inferencer, expected_type)
        if protocol is EngineProtocol.GRPC:
            assert isinstance(inferencer._transport, GrpcTransport)
    finally:
        await inferencer.close()


@pytest.mark.parametrize(
    ("concrete", "base"),
    [
        (UxSpeechAsrInferencer, AsrInferencer),
        (UxVadInferencer, VadInferencer),
        (QwenDuplexTtsInferencer, TtsInferencer),
    ],
)
def test_grpc_inferencers_inherit_capability_base_only(
    concrete: type,
    base: type,
) -> None:
    assert issubclass(concrete, base)
    assert not issubclass(concrete, GrpcTransport)


@pytest.mark.parametrize(
    "api, protocol",
    [
        (EngineApi.UX_SPEECH, EngineProtocol.HTTPS),
        (EngineApi.RESPONSES, EngineProtocol.GRPC),
    ],
)
def test_create_inferencer_rejects_unsupported_api_protocol_pair(
    api: EngineApi,
    protocol: EngineProtocol,
) -> None:
    with pytest.raises(EngineConfigurationInvalidError):
        create_inferencer(
            snapshot(api, protocol),
            max_message_bytes=4096,
        )
