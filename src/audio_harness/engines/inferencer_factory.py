from __future__ import annotations

from audio_harness.configs import EngineEndpoint
from audio_harness.engines.models import EngineApi, EngineProtocol, EngineSnapshot
from audio_harness.errors import EngineConfigurationInvalidError
from audio_harness.inferencers import Inferencer
from audio_harness.inferencers.asr.grpc import UxSpeechAsrInferencer
from audio_harness.inferencers.responses.http import ResponsesInferencer
from audio_harness.inferencers.tts.grpc import QwenDuplexTtsInferencer
from audio_harness.inferencers.vad.grpc import UxVadInferencer
from audio_harness.inferencers.embedding import AudioEmbeddingInferencer
from audio_harness.inferencers.diarization import UxSpeakerDiarizationInferencer


def create_inferencer(
    snapshot: EngineSnapshot,
    *,
    max_message_bytes: int,
) -> Inferencer:
    if (
        snapshot.protocol is EngineProtocol.HTTPS
        and snapshot.api is EngineApi.RESPONSES
    ):
        return ResponsesInferencer(snapshot)
    endpoint = EngineEndpoint(
        target=snapshot.target,
        connect_timeout_seconds=snapshot.connect_timeout_seconds,
        request_timeout_seconds=snapshot.request_timeout_seconds,
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
        and snapshot.api is EngineApi.ENGINE_FEATURE
    ):
        return AudioEmbeddingInferencer(
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
