from __future__ import annotations

import pytest

from audio_harness.pipelines.dag import (
    DuplexTtsNodeConfig,
    ResponsesAgentNodeConfig,
)
from audio_harness.pipelines.stages import (
    DuplexTtsStage,
    ResponsesAgentStage,
    SemanticVadStage,
    StageWorkflowDefinition,
    WorkflowStages,
    default_stage_workflow,
)
from audio_harness.workflow_types import WorkflowKind


def test_transcription_stages_expand_to_the_exact_runtime_topology() -> None:
    definition = StageWorkflowDefinition(
        stages=WorkflowStages(semantic_vad=SemanticVadStage())
    )

    pipeline = definition.to_pipeline(WorkflowKind.TRANSCRIPTION)

    assert [node.id for node in pipeline.nodes] == [
        "audio-input",
        "physical-vad",
        "semantic-vad",
        "asr",
        "session-output",
    ]
    assert [
        (edge.source_node, edge.source_port, edge.target_node, edge.target_port)
        for edge in pipeline.edges
    ] == [
        ("audio-input", "audio", "physical-vad", "audio"),
        ("physical-vad", "segments", "semantic-vad", "segments"),
        ("semantic-vad", "segments", "asr", "segments"),
        ("physical-vad", "events", "session-output", "events"),
        ("asr", "events", "session-output", "events"),
        ("semantic-vad", "events", "session-output", "events"),
    ]


def test_voice_agent_requires_complete_model_and_voice_configuration() -> None:
    incomplete = default_stage_workflow(WorkflowKind.VOICE_AGENT)
    with pytest.raises(ValueError, match="模型名称"):
        incomplete.to_pipeline(WorkflowKind.VOICE_AGENT)

    complete = StageWorkflowDefinition(
        stages=WorkflowStages(
            responses_agent=ResponsesAgentStage(
                config=ResponsesAgentNodeConfig(model="gpt-4.1-mini")
            ),
            duplex_tts=DuplexTtsStage(
                config=DuplexTtsNodeConfig(
                    default_voice="voice-a",
                    allowed_voices=["voice-a"],
                )
            ),
        )
    )
    pipeline = complete.to_pipeline(WorkflowKind.VOICE_AGENT)

    assert [node.id for node in pipeline.nodes][-3:] == [
        "responses-agent",
        "duplex-tts",
        "session-output",
    ]
    assert any(
        edge.source_node == "responses-agent"
        and edge.target_node == "duplex-tts"
        for edge in pipeline.edges
    )


def test_transcription_rejects_agent_only_stages() -> None:
    definition = StageWorkflowDefinition(
        stages=WorkflowStages(responses_agent=ResponsesAgentStage())
    )

    with pytest.raises(ValueError, match="转写工作流"):
        definition.to_pipeline(WorkflowKind.TRANSCRIPTION)
