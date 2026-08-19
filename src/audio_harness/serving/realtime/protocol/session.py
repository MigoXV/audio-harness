from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from openai._utils._transform import PropertyInfo
from openai.types.realtime import (
    RealtimeSessionCreateRequest,
    RealtimeTranscriptionSessionCreateRequest,
    SessionCreatedEvent,
    SessionUpdateEvent,
    SessionUpdatedEvent,
)
from openai.types.realtime.realtime_audio_config import RealtimeAudioConfig
from openai.types.realtime.realtime_audio_config_input import (
    RealtimeAudioConfigInput,
)
from openai.types.realtime.realtime_audio_formats import (
    AudioPCM,
    AudioPCMA,
    AudioPCMU,
)
from openai.types.realtime.realtime_transcription_session_audio import (
    RealtimeTranscriptionSessionAudio,
)
from openai.types.realtime.realtime_transcription_session_audio_input import (
    RealtimeTranscriptionSessionAudioInput,
)


class AhaAudioPCM(AudioPCM):
    """Audio Harness PCM extension supporting 16 and 24 kHz input."""

    rate: Literal[16_000, 24_000] | None = None
    type: Literal["audio/pcm"] | None = None


AhaAudioFormat: TypeAlias = Annotated[
    AhaAudioPCM | AudioPCMU | AudioPCMA,
    PropertyInfo(discriminator="type"),
]


class AhaRealtimeAudioConfigInput(RealtimeAudioConfigInput):
    format: AhaAudioFormat | None = None


class AhaRealtimeAudioConfig(RealtimeAudioConfig):
    input: AhaRealtimeAudioConfigInput | None = None


class AhaRealtimeSessionCreateRequest(RealtimeSessionCreateRequest):
    audio: AhaRealtimeAudioConfig | None = None


class AhaRealtimeTranscriptionSessionAudioInput(
    RealtimeTranscriptionSessionAudioInput
):
    format: AhaAudioFormat | None = None


class AhaRealtimeTranscriptionSessionAudio(
    RealtimeTranscriptionSessionAudio
):
    input: AhaRealtimeTranscriptionSessionAudioInput | None = None


class AhaRealtimeTranscriptionSessionCreateRequest(
    RealtimeTranscriptionSessionCreateRequest
):
    audio: AhaRealtimeTranscriptionSessionAudio | None = None


AhaRealtimeSession: TypeAlias = (
    AhaRealtimeSessionCreateRequest
    | AhaRealtimeTranscriptionSessionCreateRequest
)


class AhaSessionUpdateEvent(SessionUpdateEvent):
    session: AhaRealtimeSession


class AhaSessionCreatedEvent(SessionCreatedEvent):
    session: AhaRealtimeSession


class AhaSessionUpdatedEvent(SessionUpdatedEvent):
    session: AhaRealtimeSession
