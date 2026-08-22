from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class McpServerConfig(StrictModel):
    server_label: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    server_url: str = Field(min_length=1, max_length=2048)
    require_approval: Literal["always", "never"] | dict[str, Any] = "always"
    allowed_tools: list[str] | None = None
    headers_env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_url(self) -> "McpServerConfig":
        if not self.server_url.startswith(("http://", "https://")):
            raise ValueError("MCP server_url 必须使用 HTTP(S)")
        return self


class ResponsesConfig(StrictModel):
    model: str = Field(default="", max_length=200)
    thinking_enabled: bool = False
    instructions: str = Field(default="", max_length=32_768)
    max_output_tokens: int = Field(default=1024, ge=1, le=32_768)
    max_tool_rounds: int = Field(default=8, ge=1, le=32)
    max_parallel_tools: int = Field(default=4, ge=1, le=16)
    tool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    service_tools: list[str] = Field(default_factory=list)
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    history_max_items: int = Field(default=128, ge=8, le=4096)


class DuplexTtsConfig(StrictModel):
    default_voice: str = Field(default="", max_length=200)
    allowed_voices: list[str] = Field(default_factory=list)
    language: str = Field(default="Auto", min_length=1, max_length=64)
    decoder_chunk_size: int = Field(default=0, ge=0, le=1024)

    @model_validator(mode="after")
    def validate_default_voice(self) -> "DuplexTtsConfig":
        if not self.allowed_voices and not self.default_voice:
            return self
        if not self.allowed_voices:
            raise ValueError("allowed_voices 不能为空")
        if self.default_voice not in self.allowed_voices:
            raise ValueError("default_voice 必须位于 allowed_voices 中")
        if len(set(self.allowed_voices)) != len(self.allowed_voices):
            raise ValueError("allowed_voices 不能重复")
        return self


class SpeakerContextConfig(StrictModel):
    """语音智能体私有的可信说话人身份使用策略。"""

    enabled: bool = False
    speaker_prompt: str = Field(default="", max_length=32_768)


class AgentBackendBinding(StrictModel):
    backend_id: str = Field(min_length=1, max_length=100)
    cwd: str = Field(min_length=1, max_length=4096)
    agent_preset: str = Field(default="standard", min_length=1, max_length=100)
    coordinator_instructions: str = Field(default="", max_length=32_768)
    task_timeout_seconds: float = Field(default=900, gt=0, le=86_400)
    interaction_timeout_seconds: float = Field(default=120, gt=0, le=3_600)

    @model_validator(mode="after")
    def validate_cwd(self) -> "AgentBackendBinding":
        if not (
            PurePosixPath(self.cwd).is_absolute()
            or PureWindowsPath(self.cwd).is_absolute()
        ):
            raise ValueError("Agent Backend cwd 必须是远端绝对路径")
        return self


class VoiceAgentDefinition(StrictModel):
    schema_version: Literal[1, 2] = 1
    audio_workflow_id: str = Field(max_length=100)
    responses_engine_id: str | None = None
    responses: ResponsesConfig = Field(default_factory=ResponsesConfig)
    tts_engine_id: str | None = None
    tts: DuplexTtsConfig = Field(default_factory=DuplexTtsConfig)
    speaker_context: SpeakerContextConfig | None = None
    agent_backend: AgentBackendBinding | None = None

    @model_validator(mode="after")
    def validate_ready_configuration(self) -> "VoiceAgentDefinition":
        if self.schema_version == 1 and self.agent_backend is not None:
            raise ValueError("schema_version=1 不支持 Agent Backend 绑定")
        if self.responses_engine_id is not None and not self.responses.model.strip():
            raise ValueError("Responses 引擎已绑定时必须填写模型名称")
        if self.tts_engine_id is not None:
            if not self.tts.allowed_voices or not self.tts.default_voice:
                raise ValueError("TTS 引擎已绑定时必须配置允许音色和默认音色")
        return self
