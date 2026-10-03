"""推理客户端的共享联合类型。"""

from typing import TypeAlias

from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.responses.http import ResponsesInferencer
from audio_harness.inferencers.tts.base import TtsInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.inferencers.embedding.base import EngineFeatureInferencer
from audio_harness.inferencers.diarization.base import DiarizationInferencer
from audio_harness.inferencers.denoise.base import DenoiseInferencer


Inferencer: TypeAlias = (
    AsrInferencer
    | VadInferencer
    | TtsInferencer
    | ResponsesInferencer
    | EngineFeatureInferencer
    | DiarizationInferencer
    | DenoiseInferencer
)
