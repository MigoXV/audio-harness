from __future__ import annotations

from typing import NamedTuple

import numpy as np


class PcmChunk(NamedTuple):
    """带绝对采样位置的单声道 PCM S16LE 音频块。"""

    pcm_s16le: bytes
    sample_rate: int
    start_sample: int

    @property
    def end_sample(self) -> int:
        return self.start_sample + len(self.pcm_s16le) // 2


class VadAudioChunk(NamedTuple):
    """仅供物理 VAD 使用的同步原音与检测音频。"""

    original_pcm_s16le: bytes
    vad_pcm_s16le: bytes
    sample_rate: int
    start_sample: int

    @property
    def end_sample(self) -> int:
        return self.start_sample + len(self.original_pcm_s16le) // 2


def mix_pcm_s16le(
    original: bytes,
    denoised: bytes,
    denoised_weight: float,
) -> bytes:
    """按权重逐采样混合两路等长 PCM，并饱和到 int16。"""

    if len(original) != len(denoised) or len(original) % 2:
        raise ValueError("参与混合的 PCM 必须等长且为完整的 int16 采样")
    if not 0 <= denoised_weight <= 1:
        raise ValueError("denoised_weight 必须位于 0..1")
    if denoised_weight == 0:
        return original
    if denoised_weight == 1:
        return denoised
    original_samples = np.frombuffer(original, dtype="<i2").astype(np.float64)
    denoised_samples = np.frombuffer(denoised, dtype="<i2").astype(np.float64)
    mixed = np.rint(
        original_samples * (1 - denoised_weight)
        + denoised_samples * denoised_weight
    )
    np.clip(mixed, -32768, 32767, out=mixed)
    return mixed.astype("<i2").tobytes()
