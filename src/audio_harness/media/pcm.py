from __future__ import annotations

from typing import NamedTuple


class PcmChunk(NamedTuple):
    """带绝对采样位置的单声道 PCM S16LE 音频块。"""

    pcm_s16le: bytes
    sample_rate: int
    start_sample: int

    @property
    def end_sample(self) -> int:
        return self.start_sample + len(self.pcm_s16le) // 2
