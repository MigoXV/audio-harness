from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol


@dataclass(frozen=True)
class RecognitionOptions:
    sample_rate: int
    language_code: str
    enable_automatic_punctuation: bool = True
    hotwords: tuple[str, ...] = ()
    hotword_bias: float = 0.0


@dataclass(frozen=True)
class VadBoundary:
    start_sample: Optional[int]
    end_sample: Optional[int]


@dataclass(frozen=True)
class VadUpdate:
    boundaries: tuple[VadBoundary, ...]
    processed_samples: int


@dataclass(frozen=True)
class AudioSegment:
    audio: bytes
    start_sample: int
    end_sample: int
    turn_completed: bool = True


@dataclass(frozen=True)
class TranscriptionResult:
    transcript: str
    is_final: bool
    start_sample: int
    end_sample: int
    sample_rate: int
    turn_completed: bool
    language_code: Optional[str] = None
    metadata: Mapping[str, str] = field(default_factory=dict)


class AsyncVadEngine(Protocol):
    def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[VadUpdate]: ...

    async def detect_turn(
        self,
        audio: bytes,
        sample_rate: int,
    ) -> bool: ...


class AsyncAsrEngine(Protocol):
    def transcribe(
        self,
        segment: AudioSegment,
        options: RecognitionOptions,
    ) -> AsyncIterator[TranscriptionResult]: ...


class AudioHarnessError(Exception):
    """Audio Harness 可映射到外部协议的基础异常。"""


class InvalidAudioError(AudioHarnessError):
    """输入音频或配置不合法。"""


class CapacityExceededError(AudioHarnessError):
    """有界容量已经耗尽。"""


class EngineUnavailableError(AudioHarnessError):
    """下游引擎不可用。"""


class EngineDeadlineError(AudioHarnessError):
    """下游引擎请求超时。"""
