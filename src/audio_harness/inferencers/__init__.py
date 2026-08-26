from typing import TypeAlias

from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.responses.http import ResponsesInferencer
from audio_harness.inferencers.tts.base import TtsInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.inferencers.embedding import AudioEmbeddingInferencer
from audio_harness.inferencers.diarization import DiarizationInferencer
from audio_harness.inferencers.denoise import DenoiseInferencer


Inferencer: TypeAlias = (
    AsrInferencer
    | VadInferencer
    | TtsInferencer
    | ResponsesInferencer
    | AudioEmbeddingInferencer
    | DiarizationInferencer
    | DenoiseInferencer
)


__all__ = [
    "AsrInferencer",
    "AudioEmbeddingInferencer",
    "DiarizationInferencer",
    "DenoiseInferencer",
    "Inferencer",
    "TtsInferencer",
    "VadInferencer",
]
