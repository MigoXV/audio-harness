from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_serializer

NonNegativeInt: TypeAlias = Annotated[int, Field(ge=0)]
PositiveInt: TypeAlias = Annotated[int, Field(gt=0)]
NonNegativeFloat: TypeAlias = Annotated[float, Field(ge=0)]


class AhaProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AhaV1ServerEventBase(AhaProtocolModel):
    event_id: str


class AhaError(AhaProtocolModel):
    code: str
    message: str


class AhaVadProgressEvent(AhaV1ServerEventBase):
    type: Literal["x_aha.vad.progress"]
    processed_audio_ms: NonNegativeInt
    final: Literal[True] | None = None


class AhaVadPhysicalSpeechStoppedEvent(AhaV1ServerEventBase):
    type: Literal["x_aha.vad.physical_speech_stopped"]
    item_id: str
    audio_start_ms: NonNegativeInt
    audio_end_ms: NonNegativeInt
    turn_completed: Literal[False]


class AhaInputAudioTranscriptionInterimEvent(AhaV1ServerEventBase):
    type: Literal["x_aha.input_audio_transcription.interim"]
    item_id: str
    content_index: Literal[0]
    revision: PositiveInt
    transcript: str
    audio_start_ms: NonNegativeInt
    audio_end_ms: NonNegativeInt


class AhaInputAudioTranscriptionInterimClearedEvent(
    AhaV1ServerEventBase
):
    type: Literal["x_aha.input_audio_transcription.interim_cleared"]
    item_id: str
    reason: Literal["primary_transcript_started", "superseded"]


class AhaVectorObservation(AhaProtocolModel):
    point_id: str
    payload: dict[str, Any]


class AhaVectorSearchGroup(AhaProtocolModel):
    speaker_id: str
    score: float
    observation: AhaVectorObservation


class AhaVectorSearchEventBase(AhaV1ServerEventBase):
    item_id: str
    audio_start_ms: NonNegativeInt
    audio_end_ms: NonNegativeInt
    collection_name: str
    vector_name: str


class AhaVectorSearchCompletedEvent(AhaVectorSearchEventBase):
    type: Literal["x_aha.vector.search.completed"]
    groups: list[AhaVectorSearchGroup]
    latency_ms: NonNegativeFloat
    embedding: list[float] | None = None


class AhaVectorSearchFailedEvent(AhaVectorSearchEventBase):
    type: Literal["x_aha.vector.search.failed"]
    error: AhaError


class AhaDiarizationSegment(AhaProtocolModel):
    segment_id: str
    audio_start_ms: NonNegativeInt
    audio_end_ms: NonNegativeInt
    speaker: int
    local_speaker_id: str


class AhaDiarizationLocalEventBase(AhaV1ServerEventBase):
    batch_id: str
    batch_audio_start_ms: NonNegativeInt
    batch_audio_end_ms: NonNegativeInt
    speech_duration_ms: NonNegativeInt
    sequence: NonNegativeInt | None = None
    segments: list[AhaDiarizationSegment]
    latency_ms: NonNegativeFloat


class AhaDiarizationLocalDeltaEvent(AhaDiarizationLocalEventBase):
    type: Literal["x_aha.diarization.local.delta"]


class AhaDiarizationLocalCompletedEvent(AhaDiarizationLocalEventBase):
    type: Literal["x_aha.diarization.local.completed"]


class AhaDiarizationLocalFailedEvent(AhaDiarizationLocalEventBase):
    type: Literal["x_aha.diarization.local.failed"]
    error: AhaError


class AhaSpeakerTranscriptionEventBase(AhaV1ServerEventBase):
    item_id: str
    item_source_id: str
    segment_id: str
    batch_id: str
    # 实时 delta 可能早于 local diarization 快照到达；此时身份字段会被
    # exclude_none 序列化省略，因此协议上必须允许缺失。
    local_speaker_id: str | None = None
    speaker: int | None = None
    audio_start_ms: NonNegativeInt
    audio_end_ms: NonNegativeInt
    language: str | None = None


class AhaSpeakerTranscriptionDeltaEvent(
    AhaSpeakerTranscriptionEventBase
):
    type: Literal["x_aha.input_audio_transcription.speaker.delta"]
    delta: str


class AhaSpeakerTranscriptionCompletedEvent(
    AhaSpeakerTranscriptionEventBase
):
    type: Literal["x_aha.input_audio_transcription.speaker.completed"]
    transcript: str


class AhaSpeakerTranscriptionFailedEvent(
    AhaSpeakerTranscriptionEventBase
):
    type: Literal["x_aha.input_audio_transcription.speaker.failed"]
    error: AhaError


class AhaDiarizationClusterAssignment(AhaProtocolModel):
    segment_id: str
    local_speaker_id: str
    speaker_id: int


class AhaDiarizationClusterEventBase(AhaV1ServerEventBase):
    revision: PositiveInt
    replaces_revision: PositiveInt | None
    final: Literal[True] | None = None
    processed_audio_ms: NonNegativeInt | None = None

    @model_serializer(mode="wrap")
    def _serialize_cluster_event(self, handler):
        payload = handler(self)
        payload["replaces_revision"] = self.replaces_revision
        return payload


class AhaDiarizationClusterCompletedEvent(
    AhaDiarizationClusterEventBase
):
    type: Literal["x_aha.diarization.cluster.completed"]
    full_snapshot: Literal[True]
    assignments: list[AhaDiarizationClusterAssignment]


class AhaDiarizationClusterFailedEvent(AhaDiarizationClusterEventBase):
    type: Literal["x_aha.diarization.cluster.failed"]
    error: AhaError


AhaV1ServerEvent: TypeAlias = (
    AhaVadProgressEvent
    | AhaVadPhysicalSpeechStoppedEvent
    | AhaInputAudioTranscriptionInterimEvent
    | AhaInputAudioTranscriptionInterimClearedEvent
    | AhaVectorSearchCompletedEvent
    | AhaVectorSearchFailedEvent
    | AhaDiarizationLocalDeltaEvent
    | AhaDiarizationLocalCompletedEvent
    | AhaDiarizationLocalFailedEvent
    | AhaSpeakerTranscriptionDeltaEvent
    | AhaSpeakerTranscriptionCompletedEvent
    | AhaSpeakerTranscriptionFailedEvent
    | AhaDiarizationClusterCompletedEvent
    | AhaDiarizationClusterFailedEvent
)
