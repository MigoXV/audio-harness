import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi, AudioWorkflow, EngineAsset, RealtimeRoute, VoiceAgent } from "../api/types";

interface Props { api: AudioHarnessApi; refreshVersion: number; }

export function VoiceAgentPage({ api, refreshVersion }: Props) {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [workflows, setWorkflows] = useState<AudioWorkflow[]>([]);
  const [engines, setEngines] = useState<EngineAsset[]>([]);
  const [routes, setRoutes] = useState<RealtimeRoute[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [nextAgents, nextWorkflows, engineList, nextRoutes] = await Promise.all([
        api.listVoiceAgents(), api.listAudioWorkflows(), api.listEngines({ limit: 200 }), api.listRealtimeRoutes(),
      ]);
      setAgents(nextAgents); setWorkflows(nextWorkflows); setEngines(engineList.items); setRoutes(nextRoutes); setError(null);
    } catch (reason) { setError(errorMessage(reason, "语音智能体读取失败。")); }
    finally { setLoading(false); }
  }, [api]);
  useEffect(() => { const timer = window.setTimeout(() => void load(), 0); return () => window.clearTimeout(timer); }, [load, refreshVersion]);

  const prerequisites = useMemo(() => [
    { label: "已发布语音工作流", ready: workflows.some((item) => item.active_revision !== null), action: "/audio-workflows" },
    { label: "可用 Responses 引擎", ready: engines.some((item) => item.enabled && item.api === "responses"), action: "/engines" },
    { label: "可用 TTS 引擎", ready: engines.some((item) => item.enabled && item.api === "qwen_tts_duplex"), action: "/engines" },
  ], [engines, workflows]);
  const canCreate = prerequisites.every((item) => item.ready);

  return <main className="workspace-page">
    <header className="page-heading"><div><p className="eyebrow">VOICE INTELLIGENCE</p><h1>语音智能体</h1><p>在已发布语音工作流之上组合 Responses、TTS 与对话策略。</p></div><button className="primary-button" disabled={!canCreate} title={!canCreate ? "先完成页面中的三个先决条件" : undefined} onClick={() => navigate("/voice-agents/new")}>新建语音智能体</button></header>
    {error && <div className="alert alert-error"><span>{error}</span><button className="text-button" onClick={() => setError(null)}>关闭</button></div>}
    {loading ? <div className="empty-state">正在加载…</div> : agents.length === 0 ? <section className="panel agent-onboarding"><div><p className="eyebrow">GET STARTED</p><h2>创建第一个语音智能体</h2><p>语音智能体不会复制语音 DAG；它引用一个不可变的活动工作流，并额外绑定大模型与语音合成。</p></div><div className="prerequisite-list">{prerequisites.map((item) => <button key={item.label} onClick={() => navigate(item.action)}><span className={`check-mark ${item.ready ? "ready" : "missing"}`}>{item.ready ? "✓" : "→"}</span><div><strong>{item.label}</strong><small>{item.ready ? "已满足" : "前往配置"}</small></div></button>)}</div>{canCreate && <button className="primary-button" onClick={() => navigate("/voice-agents/new")}>开始四步创建</button>}</section> : <section className="panel"><div className="panel-heading"><div><h2>全部语音智能体</h2><p>{agents.length} 个独立配置</p></div><button className="secondary-button" onClick={() => void load()}>刷新</button></div><div className="workflow-table"><div className="workflow-row voice-agent-row table-header"><span>语音智能体</span><span>草稿</span><span>活动版本</span><span>Realtime 接入</span><span /></div>{agents.map((agent) => { const models = routes.filter((route) => route.target_type === "voice_agent" && route.target_id === agent.id); return <button className="workflow-row voice-agent-row" key={agent.id} onClick={() => navigate(`/voice-agents/${encodeURIComponent(agent.id)}/configure`)}><span><strong>{agent.name}</strong><small>{agent.slug}</small></span><span>r{agent.draft_revision}</span><span><span className={`status-chip ${agent.active_revision ? "status-published_healthy" : "status-unpublished"}`}>{agent.active_revision ? `v${agent.active_revision}` : "未发布"}</span></span><span>{models.length ? models.map((route) => <code key={route.model}>{route.model}</code>) : <small className="muted">未接入</small>}</span><span className="row-arrow">→</span></button>; })}</div></section>}
  </main>;
}
