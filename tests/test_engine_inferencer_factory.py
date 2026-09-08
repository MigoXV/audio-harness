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
from audio_harness.inferencers.diarization.base import DiarizationInferencer
from audio_harness.inferencers.diarization.grpc import (
    UxSpeakerDiarizationInferencer,
)
from audio_harness.inferencers.diarization import DummyDiarizationInferencer
from audio_harness.inferencers.denoise import DenoiseInferencer, UxDenoiseInferencer
from audio_harness.inferencers.embedding import (
    EngineFeatureInferencer,
    GrpcEngineFeatureInferencer,
)


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
        (EngineApi.UX_DENOISE, EngineProtocol.GRPC, UxDenoiseInferencer),
        (EngineApi.UX_SPEECH, EngineProtocol.GRPC, UxSpeechAsrInferencer),
        (EngineApi.UX_VAD, EngineProtocol.GRPC, UxVadInferencer),
        (
            EngineApi.UX_SPEAKER_DIARIZATION,
            EngineProtocol.GRPC,
            UxSpeakerDiarizationInferencer,
        ),
        (
            EngineApi.DUMMY_DIARIZE,
            EngineProtocol.DUMMY,
            DummyDiarizationInferencer,
        ),
        (
            EngineApi.ENGINE_FEATURE,
            EngineProtocol.GRPC,
            GrpcEngineFeatureInferencer,
        ),
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
        (UxDenoiseInferencer, DenoiseInferencer),
        (UxSpeechAsrInferencer, AsrInferencer),
        (UxVadInferencer, VadInferencer),
        (QwenDuplexTtsInferencer, TtsInferencer),
        (UxSpeakerDiarizationInferencer, DiarizationInferencer),
    ],
)
def test_grpc_inferencers_inherit_capability_base_only(
    concrete: type,
    base: type,
) -> None:
    assert issubclass(concrete, base)
    assert not issubclass(concrete, GrpcTransport)


def test_grpc_engine_feature_inferencer_implements_unified_capability() -> None:
    assert issubclass(GrpcEngineFeatureInferencer, EngineFeatureInferencer)


@pytest.mark.parametrize(
    "api, protocol",
    [
        (EngineApi.UX_SPEECH, EngineProtocol.HTTPS),
        (EngineApi.RESPONSES, EngineProtocol.GRPC),
        (EngineApi.DUMMY_DIARIZE, EngineProtocol.GRPC),
        (EngineApi.UX_SPEAKER_DIARIZATION, EngineProtocol.DUMMY),
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
