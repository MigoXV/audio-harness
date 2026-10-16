from __future__ import annotations

from audio_harness.configs.engine import EngineEndpoint
from audio_harness.engines.models import EngineApi, EngineProtocol, EngineSnapshot
from audio_harness.errors import EngineConfigurationInvalidError
from audio_harness.inferencers.types import Inferencer
from audio_harness.inferencers.asr.grpc import UxSpeechAsrInferencer
from audio_harness.inferencers.responses.http import ResponsesInferencer
from audio_harness.inferencers.tts.grpc import QwenDuplexTtsInferencer
from audio_harness.inferencers.vad.grpc import UxVadInferencer
from audio_harness.inferencers.embedding.grpc import GrpcEngineFeatureInferencer
from audio_harness.inferencers.embedding.dummy import DummyClusteringInferencer
from audio_harness.inferencers.diarization.dummy import DummyDiarizationInferencer
from audio_harness.inferencers.diarization.grpc import UxSpeakerDiarizationInferencer
from audio_harness.inferencers.diarization.streaming_grpc import UxStreamingDiarizationInferencer
from audio_harness.inferencers.denoise.grpc import UxDenoiseInferencer


def create_inferencer(
    snapshot: EngineSnapshot,
    *,
    max_message_bytes: int,
) -> Inferencer:
    if (
        snapshot.protocol is EngineProtocol.DUMMY
        and snapshot.api is EngineApi.DUMMY_DIARIZE
    ):
        return DummyDiarizationInferencer()
    if (
        snapshot.protocol is EngineProtocol.HTTPS
        and snapshot.api is EngineApi.RESPONSES
    ):
        return ResponsesInferencer(snapshot)
    if (
        snapshot.protocol is EngineProtocol.DUMMY
        and snapshot.api is EngineApi.DUMMY_CLUSTERING
    ):
        return DummyClusteringInferencer()
    endpoint = EngineEndpoint(
        target=snapshot.target,
        connect_timeout_seconds=snapshot.connect_timeout_seconds,
        request_timeout_seconds=snapshot.request_timeout_seconds,
    )
    if (
        snapshot.protocol is EngineProtocol.GRPC
        and snapshot.api is EngineApi.UX_DENOISE
    ):
        return UxDenoiseInferencer(
            endpoint,
            max_message_bytes=max_message_bytes,
        )
    if (
        snapshot.protocol is EngineProtocol.GRPC
        and snapshot.api is EngineApi.UX_SPEAKER_DIARIZATION
    ):
        return UxSpeakerDiarizationInferencer(
            endpoint,
            max_message_bytes=max_message_bytes,
        )
    if (
        snapshot.protocol is EngineProtocol.GRPC
        and snapshot.api is EngineApi.UX_SPEAKER_DIARIZATION_DUPLEX
    ):
        return UxStreamingDiarizationInferencer(endpoint, max_message_bytes=max_message_bytes)
    if (
        snapshot.protocol is EngineProtocol.GRPC
        and snapshot.api is EngineApi.ENGINE_FEATURE
    ):
        return GrpcEngineFeatureInferencer(
            endpoint,
            max_message_bytes=max_message_bytes,
        )
    if (
        snapshot.protocol is EngineProtocol.GRPC
        and snapshot.api is EngineApi.UX_SPEECH
    ):
        return UxSpeechAsrInferencer(
            endpoint,
            max_message_bytes=max_message_bytes,
        )
    if (
        snapshot.protocol is EngineProtocol.GRPC
        and snapshot.api is EngineApi.UX_VAD
    ):
        return UxVadInferencer(
            endpoint,
            max_message_bytes=max_message_bytes,
        )
    if (
        snapshot.protocol is EngineProtocol.GRPC
        and snapshot.api is EngineApi.QWEN_TTS_DUPLEX
    ):
        return QwenDuplexTtsInferencer(
            endpoint,
            max_message_bytes=max_message_bytes,
        )
    raise EngineConfigurationInvalidError(
        f"不支持的引擎协议/API: "
        f"{snapshot.protocol.value}/{snapshot.api.value}"
    )
