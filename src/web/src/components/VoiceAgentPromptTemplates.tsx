import { useState } from "react";

const TEMPLATES = [
  {
    id: "unlock",
    name: "仅向量解锁",
    instructions: "你是一个语音智能体。只有通过系统说话人过滤的语音才会进入当前对话。正常响应用户即可；除非用户主动询问，否则不要讨论声纹识别、匹配分数或内部实现。",
  },
  {
    id: "personalized",
    name: "按 Speaker ID 个性化",
    instructions: "你是一个个性化语音智能体。系统会为当前发言提供可信的 speaker_id。请只按照下方明确配置的映射称呼用户，不要根据 speaker_id 猜测姓名或其他资料。\n\nSpeaker ID 映射：\n- speaker-001：请在这里填写称呼和偏好",
  },
  {
    id: "allowlist",
    name: "多人物白名单",
    instructions: "你是一个面向多位授权说话人的语音智能体。系统会为每次发言提供可信的 speaker_id。请区分不同说话人，并严格按照下方映射执行对应角色和回复规则；未配置的信息一律不要猜测。\n\n说话人规则：\n- speaker-001：请填写规则\n- speaker-002：请填写规则",
  },
] as const;

interface Props {
  currentInstructions: string;
  onApply: (instructions: string) => void;
}

export function VoiceAgentPromptTemplates({ currentInstructions, onApply }: Props) {
  const [selected, setSelected] = useState(TEMPLATES[0].id);
  const apply = () => {
    const template = TEMPLATES.find((item) => item.id === selected)!;
    if (
      currentInstructions.trim()
      && !window.confirm("当前系统指令不为空，确认使用模板替换吗？")
    ) return;
    onApply(template.instructions);
  };
  return <label className="full-span">
    <span>系统指令模板</span>
    <div className="tag-editor">
      <select value={selected} onChange={(event) => setSelected(event.target.value as typeof selected)}>
        {TEMPLATES.map((template) => <option value={template.id} key={template.id}>{template.name}</option>)}
      </select>
      <button type="button" className="secondary-button" onClick={apply}>应用模板</button>
    </div>
    <small>模板会写入下方系统指令，应用后可以继续编辑。</small>
  </label>;
}
