from typing import Literal

from audio_harness.voice_agents.speaker_context import (
    SpeakerContext,
    speaker_context_from_result,
    speaker_context_instructions,
    speaker_context_policy_instructions,
)
from audio_harness.voice_agents.models import SpeakerContextConfig
from audio_harness.workflows.events import SpeakerFilterResult


def _result(
    status: Literal["matched", "rejected", "failed"],
    *,
    speaker_id: str | None = None,
    score: float | None = None,
) -> SpeakerFilterResult:
    return SpeakerFilterResult(
        start_sample=0,
        end_sample=16000,
        sample_rate=16000,
        status=status,
        reason=("matched" if status == "matched" else "no_match"),
        speaker_id=speaker_id,
        score=score,
    )


def test_matched_filter_result_becomes_trusted_speaker_context() -> None:
    context = speaker_context_from_result(
        _result("matched", speaker_id="speaker-001", score=0.91)
    )

    assert context == SpeakerContext(speaker_id="speaker-001", score=0.91)
    instructions = speaker_context_instructions(context)
    assert "speaker_id=speaker-001" in instructions
    assert "score=0.910000" in instructions
    assert "payload" not in instructions


def test_rejected_and_failed_results_do_not_create_agent_context() -> None:
    assert speaker_context_from_result(_result("rejected")) is None
    assert speaker_context_from_result(_result("failed")) is None


def test_policy_is_static_and_excludes_turn_identity() -> None:
    policy = speaker_context_policy_instructions(
        SpeakerContextConfig(
            enabled=True,
            speaker_prompt="speaker-001 使用简洁中文回复。",
        )
    )

    assert "speaker-001 使用简洁中文回复。" in policy
    assert "score=" not in policy
    assert "当前发言已通过" not in policy
