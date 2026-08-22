from __future__ import annotations

from dataclasses import dataclass

from audio_harness.workflows.events import SpeakerFilterResult
from audio_harness.voice_agents.models import SpeakerContextConfig


@dataclass(frozen=True)
class SpeakerContext:
    """语音工作流提供给语音智能体的单回合可信说话人上下文。"""

    speaker_id: str
    score: float


def speaker_context_from_result(
    result: SpeakerFilterResult,
) -> SpeakerContext | None:
    """把工作流过滤结果收敛为智能体可消费的可信身份。"""

    if (
        result.status != "matched"
        or result.speaker_id is None
        or result.score is None
    ):
        return None
    return SpeakerContext(speaker_id=result.speaker_id, score=result.score)


def speaker_context_policy_instructions(config: SpeakerContextConfig) -> str:
    """生成发布时冻结的声纹使用策略，不包含单次发言的身份数据。"""

    speaker_prompt = config.speaker_prompt.strip() or "未配置额外的声纹提示词。"
    return (
        "[Audio Harness 声纹身份上下文配置]\n"
        "当前智能体已显式启用可信说话人身份上下文。"
        "仅在当前发言匹配成功时，系统才会提供 speaker_id 与 score。\n"
        "声纹提示词："
        + speaker_prompt
        + "\n不要猜测未配置的身份资料，不要主动披露声纹、分数、"
        "向量、候选列表或内部实现，也不要将匹配描述为账户级身份认证。"
    )


def speaker_context_instructions(
    context: SpeakerContext,
    *,
    policy: str | None = None,
) -> str:
    """生成系统维护的上下文约束，不依赖用户手写提示词。"""

    return (
        ("\n\n" + policy if policy else "")
        + "\n\n[Audio Harness 可信说话人上下文]\n"
        "当前发言已通过内部说话人过滤。"
        f"speaker_id={context.speaker_id}，score={context.score:.6f}。"
        "仅将这些字段用于当前发言的身份上下文；只有用户指令明确"
        "定义了 speaker_id 的含义时才能据此个性化。不要猜测任何"
        "其他个人资料，不要主动披露声纹、向量、匹配分数或内部机制，"
        "也不要把本次匹配描述为账户级身份认证。"
    )
