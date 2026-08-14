from __future__ import annotations

from typing import NamedTuple


class SpeechStarted(NamedTuple):
    start_sample: int
    sample_rate: int


class SpeechStopped(NamedTuple):
    start_sample: int
    end_sample: int
    sample_rate: int
