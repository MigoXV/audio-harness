from audio_harness.inferencers.diarization.base import DiarizationInferencer
from audio_harness.inferencers.diarization.grpc import (
    UxSpeakerDiarizationInferencer,
)

__all__ = ["DiarizationInferencer", "UxSpeakerDiarizationInferencer"]
