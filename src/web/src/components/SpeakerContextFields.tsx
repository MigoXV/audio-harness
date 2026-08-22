import { useState } from "react";
import { Link } from "react-router-dom";

import type { VoiceAgentDefinition } from "../api/types";
import type { VoiceAgentWorkflowInterface } from "../features/voiceAgents/useVoiceAgentWorkflowInterface";

const TEMPLATES = [
  {
    id: "personalized",
    name: "按 Speaker ID 个性化",
    instructions: "只按以下明确映射称呼或服务用户，未配置的信息不要猜测。\n\n- speaker-001：请填写称呼和偏好",
  },
  {
    id: "allowlist",
    name: "多人物规则",
    instructions: "根据以下明确的说话人规则执行；未列出的身份不要推断个人资料。\n\n- speaker-001：请填写规则\n- speaker-002：请填写规则",
  },
] as const;

interface Props {
  definition: VoiceAgentDefinition;
  workflowInterface: VoiceAgentWorkflowInterface;
  contextPreview?: string | null;
  contextPreviewStale?: boolean;
  onChange: (definition: VoiceAgentDefinition) => void;
}

export function SpeakerContextFields({
  definition, workflowInterface, contextPreview, contextPreviewStale = false, onChange,
}: Props) {
  const [selectedTemplate, setSelectedTemplate] = useState(TEMPLATES[0].id);
  const legacy = definition.speaker_context == null;
  const config = definition.speaker_context ?? { enabled: true, speaker_prompt: "" };
  const update = (patch: Partial<typeof config>) => onChange({
    ...definition,
    speaker_context: { ...config, ...patch },
  });
  const applyTemplate = () => {
    const template = TEMPLATES.find((item) => item.id === selectedTemplate)!;
    if (config.speaker_prompt.trim() && !window.confirm("当前声纹提示词不为空，确认使用模板替换吗？")) return;
    update({ speaker_prompt: template.instructions });
  };
  const sourceText = ({
    unbound: "请先绑定语音工作流。",
    loading: "正在检查已发布工作流的 speaker_filter 出口…",
    available: "已找到 speaker_filter 出口；匹配成功时才会注入当前回合。",
    unavailable: "当前工作流未暴露 speaker_filter 出口；启用后将不能发布。",
    error: "无法确认 speaker_filter 出口；请检查工作流后重试。",
  } as const)[workflowInterface.status];

  return <section className="agent-speaker-context">
    <header><div><span>智能体私有配置</span><h3>声纹提示词</h3></div></header>
    <p>它与 Responses 的系统提示词独立。语音工作流只提供 <code>speaker_filter</code> 结果；匹配成功时才附加此提示词和可信身份。</p>
    {legacy && <div className="inline-notice">此草稿沿用旧版自动注入行为。修改开关或声纹提示词后会迁移为显式配置。</div>}
    <label className="switch-control">
      <input type="checkbox" checked={config.enabled} onChange={(event) => update({ enabled: event.target.checked })} />
      <span>为匹配成功的当前发言注入可信身份</span>
    </label>
    <small className={workflowInterface.status === "available" || !config.enabled ? "field-hint" : "field-hint warning"}>{sourceText}</small>
    {config.enabled && <>
      <label><span>声纹提示词</span><textarea value={config.speaker_prompt} onChange={(event) => update({ speaker_prompt: event.target.value })} placeholder="例如：speaker-001 是小王，优先使用简洁的中文回复。" /></label>
      <label><span>声纹提示词模板</span><div className="tag-editor"><select value={selectedTemplate} onChange={(event) => setSelectedTemplate(event.target.value as typeof selectedTemplate)}>{TEMPLATES.map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}</select><button type="button" className="secondary-button" onClick={applyTemplate}>应用模板</button></div></label>
      <section className="speaker-context-preview">
        <h3>将冻结的声纹提示词{contextPreviewStale ? "（待保存）" : ""}</h3>
        <p>发布后此提示词会随活动版本固定；实际的 <code>speaker_id</code> 与分数只在匹配成功的当前回合补充。</p>
        <pre>{contextPreview ?? "保存草稿后可查看服务端生成的策略预览。"}</pre>
      </section>
    </>}
    {workflowInterface.error && <div className="inline-error">{workflowInterface.error}</div>}
    {definition.audio_workflow_id && <Link className="secondary-button" to={`/audio-workflows/${encodeURIComponent(definition.audio_workflow_id)}/build`}>打开语音工作流</Link>}
  </section>;
}
