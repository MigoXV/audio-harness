from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from audio_harness.configs.base import StrictModel


class WorkflowConfig(StrictModel):
    min_chunk_bytes: int = Field(default=640, ge=2)
    min_segment_seconds: float = Field(default=0.5, ge=0)
    max_segment_seconds: float = Field(default=60.0, gt=0)
    audio_buffer_seconds: float = Field(default=120.0, gt=0)
    speech_pre_roll_seconds: float = Field(default=0.3, ge=0)
    default_language_code: str = Field(default="zh-CN", min_length=1)
    audio_queue_capacity: int = Field(default=32, gt=0)
    segment_queue_capacity: int = Field(default=8, gt=0)
    result_queue_capacity: int = Field(default=32, gt=0)

    @model_validator(mode="after")
    def validate_pcm_alignment(self) -> "WorkflowConfig":
        if self.min_chunk_bytes % 2:
            raise ValueError("min_chunk_bytes 必须为偶数，以保持 PCM S16LE 对齐")
        required_buffer = self.max_segment_seconds + self.speech_pre_roll_seconds
        if self.audio_buffer_seconds < required_buffer:
            raise ValueError(
                "audio_buffer_seconds 不能小于 "
                "max_segment_seconds + speech_pre_roll_seconds"
            )
        return self


class WorkflowRuntimeConfig(StrictModel):
    min_chunk_bytes: int = Field(default=640, ge=2)
    default_language_code: str = Field(default="zh-CN", min_length=1)
    audio_queue_capacity: int = Field(default=32, gt=0)
    segment_queue_capacity: int = Field(default=8, gt=0)
    result_queue_capacity: int = Field(default=32, gt=0)
    received_audio_cache_enabled: bool = False
    received_audio_cache_block_seconds: int = Field(default=5, ge=1, le=60)
    received_audio_cache_file_seconds: int = Field(default=300, ge=1, le=3600)

    @field_validator("min_chunk_bytes")
    @classmethod
    def validate_pcm_alignment(cls, value: int) -> int:
        if value % 2:
            raise ValueError(
                "min_chunk_bytes 必须为偶数，以保持 PCM S16LE 对齐"
            )
        return value

    @model_validator(mode="after")
    def validate_received_audio_cache_periods(self) -> "WorkflowRuntimeConfig":
        if (
            self.received_audio_cache_file_seconds
            < self.received_audio_cache_block_seconds
        ):
            raise ValueError("音频缓存文件周期不能小于块周期")
        if (
            self.received_audio_cache_file_seconds
            % self.received_audio_cache_block_seconds
        ):
            raise ValueError("音频缓存文件周期必须是块周期的整数倍")
        return self
