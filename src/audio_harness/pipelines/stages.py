from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from audio_harness.pipelines.dag import (
    DuplexTtsNodeConfig,
    FailurePolicy,
    PhysicalVadNodeConfig,
    PipelineDefinition,
    PipelineEdge,
    PipelineNode,
    ResponsesAgentNodeConfig,
    SemanticVadNodeConfig,
)
from audio_harness.workflow_types import WorkflowKind


class StrictStageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyStageConfig(StrictStageModel):
    pass


class PhysicalVadStage(StrictStageModel):
    engine_id: str | None = None
    config: PhysicalVadNodeConfig = Field(default_factory=PhysicalVadNodeConfig)


class SemanticVadStage(StrictStageModel):
    engine_id: str | None = None
    config: SemanticVadNodeConfig = Field(default_factory=SemanticVadNodeConfig)
    failure_policy: FailurePolicy = FailurePolicy.FAIL_OPEN

    @model_validator(mode="after")
    def validate_failure_policy(self) -> "SemanticVadStage":
        if self.failure_policy not in {
            FailurePolicy.FAIL_OPEN,
            FailurePolicy.FAIL_PIPELINE,
        }:
            raise ValueError("语义 VAD 失败策略必须是 fail_open 或 fail_pipeline")
        return self


class AsrStage(StrictStageModel):
    engine_id: str | None = None
    config: EmptyStageConfig = Field(default_factory=EmptyStageConfig)


class ResponsesAgentStage(StrictStageModel):
    engine_id: str | None = None
    config: ResponsesAgentNodeConfig = Field(
        default_factory=ResponsesAgentNodeConfig
    )


class DuplexTtsStage(StrictStageModel):
    engine_id: str | None = None
    config: DuplexTtsNodeConfig = Field(default_factory=DuplexTtsNodeConfig)


class WorkflowStages(StrictStageModel):
    physical_vad: PhysicalVadStage = Field(default_factory=PhysicalVadStage)
    semantic_vad: SemanticVadStage | None = None
    asr: AsrStage = Field(default_factory=AsrStage)
    responses_agent: ResponsesAgentStage | None = None
    duplex_tts: DuplexTtsStage | None = None


class StageWorkflowDefinition(StrictStageModel):
    """控制面持久化的约束式工作流定义。

    输入和输出属于固定运行边界，不作为用户可删除的阶段保存。拓扑只由工作流
    类型和可选语义 VAD 决定，因此控制面展示与运行时执行不会产生歧义。
    """

    schema_version: Literal[2] = 2
    stages: WorkflowStages = Field(default_factory=WorkflowStages)

    def to_pipeline(self, kind: WorkflowKind) -> PipelineDefinition:
        stages = self.stages
        if kind is WorkflowKind.TRANSCRIPTION:
            if stages.responses_agent is not None or stages.duplex_tts is not None:
                raise ValueError("转写工作流不能配置 Responses 或 TTS 阶段")
        elif stages.responses_agent is None or stages.duplex_tts is None:
            raise ValueError("语音 Agent 工作流必须配置 Responses 和 TTS 阶段")
        if kind is WorkflowKind.VOICE_AGENT:
            assert stages.responses_agent is not None
            assert stages.duplex_tts is not None
            if not stages.responses_agent.config.model.strip():
                raise ValueError("Responses 阶段必须填写模型名称")
            if not stages.duplex_tts.config.allowed_voices:
                raise ValueError("TTS 阶段必须配置至少一个允许音色")
            if not stages.duplex_tts.config.default_voice:
                raise ValueError("TTS 阶段必须选择默认音色")

        nodes = [
            PipelineNode(id="audio-input", type="audio_input"),
            PipelineNode(
                id="physical-vad",
                type="physical_vad",
                engine_id=stages.physical_vad.engine_id,
                config=stages.physical_vad.config.model_dump(mode="json"),
            ),
        ]
        edges = [
            PipelineEdge(
                id="audio-input-to-physical-vad",
                source_node="audio-input",
                source_port="audio",
                target_node="physical-vad",
                target_port="audio",
            )
        ]
        segment_source = "physical-vad"
        if stages.semantic_vad is not None:
            nodes.append(
                PipelineNode(
                    id="semantic-vad",
                    type="semantic_vad",
                    engine_id=stages.semantic_vad.engine_id,
                    config=stages.semantic_vad.config.model_dump(mode="json"),
                    failure_policy=stages.semantic_vad.failure_policy,
                )
            )
            edges.append(
                PipelineEdge(
                    id="physical-vad-to-semantic-vad",
                    source_node="physical-vad",
                    source_port="segments",
                    target_node="semantic-vad",
                    target_port="segments",
                )
            )
            segment_source = "semantic-vad"

        nodes.append(
            PipelineNode(
                id="asr",
                type="asr",
                engine_id=stages.asr.engine_id,
                config=stages.asr.config.model_dump(mode="json"),
            )
        )
        edges.append(
            PipelineEdge(
                id=f"{segment_source}-to-asr",
                source_node=segment_source,
                source_port="segments",
                target_node="asr",
                target_port="segments",
            )
        )

        if kind is WorkflowKind.VOICE_AGENT:
            assert stages.responses_agent is not None
            assert stages.duplex_tts is not None
            nodes.extend(
                [
                    PipelineNode(
                        id="responses-agent",
                        type="responses_agent",
                        engine_id=stages.responses_agent.engine_id,
                        config=stages.responses_agent.config.model_dump(mode="json"),
                    ),
                    PipelineNode(
                        id="duplex-tts",
                        type="duplex_tts",
                        engine_id=stages.duplex_tts.engine_id,
                        config=stages.duplex_tts.config.model_dump(mode="json"),
                    ),
                ]
            )
            edges.extend(
                [
                    PipelineEdge(
                        id="asr-to-responses-agent",
                        source_node="asr",
                        source_port="events",
                        target_node="responses-agent",
                        target_port="events",
                    ),
                    PipelineEdge(
                        id="responses-agent-to-duplex-tts",
                        source_node="responses-agent",
                        source_port="events",
                        target_node="duplex-tts",
                        target_port="events",
                    ),
                ]
            )

        nodes.append(PipelineNode(id="session-output", type="session_output"))
        event_sources = ["physical-vad", "asr"]
        if stages.semantic_vad is not None:
            event_sources.append("semantic-vad")
        if kind is WorkflowKind.VOICE_AGENT:
            event_sources.extend(["responses-agent", "duplex-tts"])
        edges.extend(
            PipelineEdge(
                id=f"{source}-to-session-output",
                source_node=source,
                source_port="events",
                target_node="session-output",
                target_port="events",
            )
            for source in event_sources
        )
        return PipelineDefinition(nodes=nodes, edges=edges)


WorkflowDefinition = StageWorkflowDefinition | PipelineDefinition


def parse_workflow_definition(value: object) -> WorkflowDefinition:
    if isinstance(value, (StageWorkflowDefinition, PipelineDefinition)):
        return value
    if isinstance(value, dict) and value.get("schema_version") == 2:
        return StageWorkflowDefinition.model_validate(value)
    return PipelineDefinition.model_validate(value)


def expanded_pipeline(
    definition: WorkflowDefinition,
    kind: WorkflowKind,
) -> PipelineDefinition:
    if isinstance(definition, StageWorkflowDefinition):
        return definition.to_pipeline(kind)
    return definition


def default_stage_workflow(kind: WorkflowKind) -> StageWorkflowDefinition:
    stages = WorkflowStages()
    if kind is WorkflowKind.VOICE_AGENT:
        stages.responses_agent = ResponsesAgentStage()
        stages.duplex_tts = DuplexTtsStage()
    return StageWorkflowDefinition(stages=stages)
