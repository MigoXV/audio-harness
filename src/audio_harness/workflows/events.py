from __future__ import annotations

from typing import NamedTuple
from dataclasses import dataclass
from typing import Any, Literal


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


@dataclass(frozen=True)
class DiarizationSegment:
    segment_id: str
    start_sample: int
    end_sample: int
    speaker: int
    local_speaker_id: str


@dataclass(frozen=True)
class DiarizationLocalResult:
    phase: Literal["delta", "completed", "failed"]
    batch_id: str
    sequence: int | None
    batch_start_sample: int
    batch_end_sample: int
    speech_samples: int
    sample_rate: int
    segments: tuple[DiarizationSegment, ...]
    latency_ms: float
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class SpeakerTranscriptionResult:
    phase: Literal["delta", "completed", "failed"]
    item_source_id: str
    segment_id: str
    batch_id: str
    local_speaker_id: str | None
    speaker: int | None
    source_start_sample: int
    source_end_sample: int
    start_sample: int
    end_sample: int
    sample_rate: int
    text: str
    language_code: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class DiarizationClusterAssignment:
    segment_id: str
    local_speaker_id: str
    speaker_id: int


@dataclass(frozen=True)
class DiarizationClusterResult:
    phase: Literal["completed", "failed"]
    revision: int
    replaces_revision: int | None
    assignments: tuple[DiarizationClusterAssignment, ...]
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.revision <= 0:
            raise ValueError("聚类 revision 必须大于 0")
        if self.replaces_revision is not None and not (
            0 < self.replaces_revision < self.revision
        ):
            raise ValueError("replaces_revision 必须小于当前 revision")
        segment_ids = [item.segment_id for item in self.assignments]
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("聚类快照不能重复包含同一 segment_id")
        if self.phase == "failed" and self.assignments:
            raise ValueError("失败的聚类事件不能携带 assignments")
