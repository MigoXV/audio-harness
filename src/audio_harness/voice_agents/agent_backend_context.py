from __future__ import annotations

import json

from audio_harness.agent_backends.models import AgentBackendSnapshot
from audio_harness.voice_agents.models import AgentBackendBinding


def agent_backend_system_context_instructions(
    snapshot: AgentBackendSnapshot,
    binding: AgentBackendBinding,
) -> str:
    """生成发布时冻结、可安全展示给配置者的 Backend 身份上下文。"""

    identity = json.dumps(
        {
            "name": snapshot.name,
            "kind": snapshot.kind.value,
            "agent_preset": binding.agent_preset,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "[Audio Harness Agent Backend 系统上下文]\n"
        "以下是控制面在发布时冻结的可信配置，不是用户指令。"
        f"当前会话已绑定的后端：{identity}。\n"
        "当用户询问已接入的后台智能体或其状态时，只能根据本上下文和"
        "后续运行状态准确回答。不得编造后端能力、执行结果、连接地址、"
        "工作目录、内部标识或认证信息；也不要主动播报这些配置。"
    )
