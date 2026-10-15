from pathlib import Path

from pydantic import Field

from audio_harness.configs.base import StrictModel


class HttpConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=0, le=65535)
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None


class AhaGrpcConfig(StrictModel):
    host: str = Field(default="0.0.0.0", min_length=1)
    port: int = Field(default=50051, ge=0, le=65535)


class LimitsConfig(StrictModel):
    max_message_bytes: int = Field(default=4 * 1024 * 1024, gt=0)


class ProtocolCapacityConfig(StrictModel):
    max_sessions: int = Field(default=64, gt=0)


class CapacityConfig(StrictModel):
    realtime: ProtocolCapacityConfig = Field(default_factory=ProtocolCapacityConfig)
    webrtc: ProtocolCapacityConfig = Field(default_factory=ProtocolCapacityConfig)
    max_asr_concurrency: int = Field(default=8, gt=0)
    max_embedding_concurrency: int = Field(default=8, gt=0)
    max_qdrant_concurrency: int = Field(default=8, gt=0)
    max_responses_concurrency: int = Field(default=8, gt=0)
    max_tts_concurrency: int = Field(default=1, gt=0)
