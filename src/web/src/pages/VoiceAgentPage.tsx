import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi, AudioWorkflow, EngineAsset, VoiceAgent } from "../api/types";

interface Props { api: AudioHarnessApi; refreshVersion: number; }

export function VoiceAgentPage({ api, refreshVersion }: Props) {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [workflows, setWorkflows] = useState<AudioWorkflow[]>([]);
  const [engines, setEngines] = useState<EngineAsset[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [nextAgents, nextWorkflows, engineList] = await Promise.all([
        api.listVoiceAgents(), api.listAudioWorkflows(), api.listEngines({ limit: 200 }),
      ]);
      setAgents(nextAgents); setWorkflows(nextWorkflows); setEngines(engineList.items); setError(null);
    } catch (reason) { setError(errorMessage(reason, "语音智能体读取失败。")); }
    finally { setLoading(false); }
  }, [api]);
  useEffect(() => { const timer = window.setTimeout(() => void load(), 0); return () => window.clearTimeout(timer); }, [load, refreshVersion]);

  const prerequisites = useMemo(() => [
    { label: "已发布语音工作流", ready: workflows.some((item) => item.active_revision !== null), action: "/audio-workflows" },
    { label: "可用 Responses 引擎", ready: engines.some((item) => item.enabled && item.api === "responses"), action: "/engines" },
    { label: "可用 TTS 引擎", ready: engines.some((item) => item.enabled && item.api === "qwen_tts_duplex"), action: "/engines" },
  ], [engines, workflows]);
  const routes: { target_type: string; target_id: string; model: string }[] = [];
  return <main className="workspace-page object-list-page">
    <header className="page-heading"><div><h1>语音智能体</h1><p>在固定拓扑画布中组合语音工作流、Responses、TTS 与可选 Agent Backend。</p></div><button className="primary-button" onClick={() => navigate("/voice-agents/new")}>新建语音智能体</button></header>
    {error && <div className="alert alert-error"><span>{error}</span><button className="text-button" onClick={() => setError(null)}>关闭</button></div>}
    {loading ? <div className="empty-state">正在加载…</div> : agents.length === 0 ? <section className="panel agent-onboarding"><div><p className="eyebrow">GET STARTED</p><h2>创建第一个语音智能体</h2><p>可以先创建空白草稿，再在构建画布中逐项补齐资源；发布前会执行完整校验。</p><button className="primary-button" onClick={() => navigate("/voice-agents/new")}>创建空白草稿</button></div><div className="prerequisite-list">{prerequisites.map((item) => <button key={item.label} onClick={() => navigate(item.action)}><span className={`check-mark ${item.ready ? "ready" : "missing"}`}>{item.ready ? "✓" : "→"}</span><div><strong>{item.label}</strong><small>{item.ready ? "已满足" : "发布前需要配置"}</small></div></button>)}</div></section> : <section className="panel object-list-panel"><div className="panel-heading"><div><h2>智能体列表</h2><p>{agents.length} 个独立配置</p></div><button className="secondary-button" onClick={() => void load()}>刷新</button></div><div className="workflow-table"><div className="workflow-row voice-agent-row table-header"><span>语音智能体</span><span>草稿</span><span>活动版本</span><span>Realtime 接入</span><span /></div><div className="object-records" tabIndex={0} role="region" aria-label="智能体记录">{agents.map((agent) => { const models = routes.filter((route) => route.target_type === "voice_agent" && route.target_id === agent.id); return <button className="workflow-row voice-agent-row" key={agent.id} onClick={() => navigate(`/voice-agents/${encodeURIComponent(agent.id)}/build`)}><span><strong>{agent.name}</strong><small>{agent.slug}</small></span><span>r{agent.draft_revision}</span><span><span className={`status-chip ${agent.active_revision ? "status-published_healthy" : "status-unpublished"}`}>{agent.active_revision ? `v${agent.active_revision}` : "未发布"}</span></span><span>{models.length ? models.map((route) => <code key={route.model}>{route.model}</code>) : <small className="muted">未接入</small>}</span><span className="row-arrow">→</span></button>; })}</div></div></section>}
  </main>;
}
