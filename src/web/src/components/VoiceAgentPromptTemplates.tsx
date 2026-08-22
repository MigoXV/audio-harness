import { useState } from "react";

const TEMPLATES = [
  {
    id: "assistant",
    name: "通用语音助手",
    instructions: "你是一个可靠、简洁的语音智能体。先直接回答用户的问题；需要澄清时只提出一个具体问题。不要主动讨论系统配置或内部实现。",
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
    <span>系统提示词模板</span>
    <div className="tag-editor">
      <select value={selected} onChange={(event) => setSelected(event.target.value as typeof selected)}>
        {TEMPLATES.map((template) => <option value={template.id} key={template.id}>{template.name}</option>)}
      </select>
      <button type="button" className="secondary-button" onClick={apply}>应用模板</button>
    </div>
    <small>声纹身份规则在「语音工作流」节点内单独配置；这里仅定义通用回复策略。</small>
  </label>;
}
