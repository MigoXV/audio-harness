import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi } from "../api/types";
import { defaultVoiceAgentDefinition, normalizeSlug } from "../features/voiceAgents/form";

interface Props { api: AudioHarnessApi; }

export function VoiceAgentCreatePage({ api }: Props) {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const normalizedSlug = normalizeSlug(slug);

  const create = async () => {
    if (!name.trim() || !normalizedSlug) return;
    setBusy(true);
    setError(null);
    try {
      const agent = await api.createVoiceAgent({
        slug: normalizedSlug,
        name: name.trim(),
        description: null,
        definition: defaultVoiceAgentDefinition(),
      });
      navigate(`/voice-agents/${encodeURIComponent(agent.id)}/build`);
    } catch (reason) {
      setError(errorMessage(reason, "语音智能体创建失败。"));
    } finally {
      setBusy(false);
    }
  };

  return <main className="workspace-page voice-agent-create-page">
    <header className="page-heading"><div><h1>新建语音智能体</h1><p>先创建一个空白草稿，再在固定拓扑画布中逐节点完成配置。</p></div><button className="text-button" onClick={() => navigate("/voice-agents")}>返回列表</button></header>
    {error && <div className="alert alert-error"><span>{error}</span><button className="text-button" onClick={() => setError(null)}>关闭</button></div>}
    <section className="panel voice-agent-create-card">
      <div className="panel-heading"><div><h2>基本信息</h2><p>草稿不会自动发布，也不会创建 Realtime 路由。</p></div></div>
      <div className="form-grid two-columns">
        <label><span>名称</span><input autoFocus value={name} onChange={(event) => { const value = event.target.value; setName(value); if (!slugEdited) setSlug(normalizeSlug(value)); }} placeholder="客服助手" /></label>
        <label><span>Slug</span><input value={slug} onChange={(event) => { setSlugEdited(true); setSlug(normalizeSlug(event.target.value)); }} placeholder="support-agent" /><small>{normalizedSlug || (name.trim() ? "名称无法生成英文 Slug，请手动填写" : "小写字母、数字和连字符")}</small></label>
      </div>
      <div className="panel-actions"><button className="secondary-button" disabled={busy} onClick={() => navigate("/voice-agents")}>取消</button><button className="primary-button" disabled={busy || !name.trim() || !normalizedSlug} onClick={() => void create()}>{busy ? "正在创建…" : "创建并进入构建"}</button></div>
    </section>
  </main>;
}
