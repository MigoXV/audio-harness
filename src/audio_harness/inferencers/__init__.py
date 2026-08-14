from typing import TypeAlias

from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.responses.http import ResponsesInferencer
from audio_harness.inferencers.tts.base import TtsInferencer
from audio_harness.inferencers.vad.base import VadInferencer


Inferencer: TypeAlias = (
    AsrInferencer
    | VadInferencer
    | TtsInferencer
    | ResponsesInferencer
)


__all__ = [
    "AsrInferencer",
    "Inferencer",
    "TtsInferencer",
    "VadInferencer",
]
