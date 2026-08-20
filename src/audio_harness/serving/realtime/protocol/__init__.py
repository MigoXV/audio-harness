from __future__ import annotations

from typing import TypeAlias

from openai.types.realtime import RealtimeClientEvent, RealtimeServerEvent
from pydantic import TypeAdapter

from .aha_v1 import (
    AhaDiarizationClusterAssignment,
    AhaDiarizationClusterCompletedEvent,
    AhaDiarizationClusterFailedEvent,
    AhaDiarizationLocalCompletedEvent,
    AhaDiarizationLocalDeltaEvent,
    AhaDiarizationLocalFailedEvent,
    AhaDiarizationSegment,
    AhaError,
    AhaAgentBackendApprovalResponseEvent,
    AhaAgentBackendCancelEvent,
    AhaAgentBackendQuestionResponseEvent,
    AhaInputAudioTranscriptionInterimClearedEvent,
    AhaInputAudioTranscriptionInterimEvent,
    AhaSpeakerTranscriptionCompletedEvent,
    AhaSpeakerTranscriptionDeltaEvent,
    AhaSpeakerTranscriptionFailedEvent,
    AhaSpeakerFilterFailedEvent,
    AhaSpeakerFilterMatchedEvent,
    AhaSpeakerFilterRejectedEvent,
    AhaV1ServerEvent,
    AhaV1ServerEventBase,
    AhaV1ClientEvent,
    AhaVadPhysicalSpeechStoppedEvent,
    AhaVadProgressEvent,
    AhaVectorObservation,
    AhaVectorSearchCompletedEvent,
    AhaVectorSearchFailedEvent,
    AhaVectorSearchGroup,
)
from .session import (
    AhaAudioPCM,
    AhaRealtimeSession,
    AhaRealtimeSessionCreateRequest,
    AhaRealtimeTranscriptionSessionCreateRequest,
    AhaSessionCreatedEvent,
    AhaSessionUpdateEvent,
    AhaSessionUpdatedEvent,
)

AhaRealtimeClientEvent: TypeAlias = (
    AhaSessionUpdateEvent | AhaV1ClientEvent | RealtimeClientEvent
)
AhaRealtimeServerEvent: TypeAlias = (
    AhaSessionCreatedEvent
    | AhaSessionUpdatedEvent
    | AhaV1ServerEvent
    | RealtimeServerEvent
)

AHA_REALTIME_SESSION_ADAPTER = TypeAdapter(AhaRealtimeSession)
AHA_REALTIME_CLIENT_EVENT_ADAPTER = TypeAdapter(AhaRealtimeClientEvent)
AHA_REALTIME_SERVER_EVENT_ADAPTER = TypeAdapter(AhaRealtimeServerEvent)

__all__ = [
    "AHA_REALTIME_CLIENT_EVENT_ADAPTER",
    "AHA_REALTIME_SERVER_EVENT_ADAPTER",
    "AHA_REALTIME_SESSION_ADAPTER",
    "AhaAudioPCM",
    "AhaAgentBackendApprovalResponseEvent",
    "AhaAgentBackendCancelEvent",
    "AhaAgentBackendQuestionResponseEvent",
    "AhaDiarizationClusterAssignment",
    "AhaDiarizationClusterCompletedEvent",
    "AhaDiarizationClusterFailedEvent",
    "AhaDiarizationLocalCompletedEvent",
    "AhaDiarizationLocalDeltaEvent",
    "AhaDiarizationLocalFailedEvent",
    "AhaDiarizationSegment",
    "AhaError",
    "AhaInputAudioTranscriptionInterimClearedEvent",
    "AhaInputAudioTranscriptionInterimEvent",
    "AhaRealtimeClientEvent",
    "AhaRealtimeServerEvent",
    "AhaRealtimeSession",
    "AhaRealtimeSessionCreateRequest",
    "AhaRealtimeTranscriptionSessionCreateRequest",
    "AhaSessionCreatedEvent",
    "AhaSessionUpdateEvent",
    "AhaSessionUpdatedEvent",
    "AhaSpeakerTranscriptionCompletedEvent",
    "AhaSpeakerTranscriptionDeltaEvent",
    "AhaSpeakerTranscriptionFailedEvent",
    "AhaSpeakerFilterFailedEvent",
    "AhaSpeakerFilterMatchedEvent",
    "AhaSpeakerFilterRejectedEvent",
    "AhaV1ServerEvent",
    "AhaV1ServerEventBase",
    "AhaVadPhysicalSpeechStoppedEvent",
    "AhaVadProgressEvent",
    "AhaVectorObservation",
    "AhaVectorSearchCompletedEvent",
    "AhaVectorSearchFailedEvent",
    "AhaVectorSearchGroup",
]
