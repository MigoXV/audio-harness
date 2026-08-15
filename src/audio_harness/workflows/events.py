from __future__ import annotations

from typing import NamedTuple
from dataclasses import dataclass
from typing import Any


class SpeechStarted(NamedTuple):
    start_sample: int
    sample_rate: int


class SpeechStopped(NamedTuple):
    start_sample: int
    end_sample: int
    sample_rate: int


@dataclass(frozen=True)
class AudioEmbeddingResult:
    start_sample: int
    end_sample: int
    sample_rate: int
    embedding: tuple[float, ...] | None
    latency_ms: float = 0.0
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class VectorSearchResult:
    start_sample: int
    end_sample: int
    sample_rate: int
    collection_name: str
    vector_name: str
    groups: tuple[dict[str, Any], ...]
    latency_ms: float
    embedding: tuple[float, ...] | None = None
    error_code: str | None = None
    error_message: str | None = None
