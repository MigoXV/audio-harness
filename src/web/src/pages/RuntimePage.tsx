import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, errorMessage } from "../api/client";
import type { AudioHarnessApi, AudioWorkflow, CapacitySettings, RealtimeRoute, RuntimeOverview, RuntimeStatus, VoiceAgent, WorkflowSettings } from "../api/types";

interface Props { api: AudioHarnessApi; refreshVersion: number; }
const modelPattern = /^[a-z0-9][a-z0-9._-]*$/;

export function RuntimePage({ api, refreshVersion }: Props) {
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [overview, setOverview] = useState<RuntimeOverview | null>(null);
  const [capacity, setCapacity] = useState<CapacitySettings | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowSettings | null>(null);
  const [draftCapacity, setDraftCapacity] = useState<CapacitySettings | null>(null);
  const [draftWorkflow, setDraftWorkflow] = useState<WorkflowSettings | null>(null);
  const [audioWorkflows, setAudioWorkflows] = useState<AudioWorkflow[]>([]);
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [routes, setRoutes] = useState<RealtimeRoute[]>([]);
  const [grpcSelection, setGrpcSelection] = useState("");
  const [routeModel, setRouteModel] = useState("");
  const [routeTarget, setRouteTarget] = useState("");
  const [routeVisibility, setRouteVisibility] = useState<"public" | "internal">("public");
  const [routeConflict, setRouteConflict] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextRuntime, nextOverview, nextCapacity, nextWorkflow, nextAudioWorkflows, nextAgents, nextRoutes] = await Promise.all([
        api.getRuntimeStatus(), api.getRuntimeOverview(), api.getCapacitySettings(), api.getWorkflowSettings(), api.listAudioWorkflows(), api.listVoiceAgents(), api.listRealtimeRoutes(),
      ]);
      const normalizedCapacity = { ...nextCapacity, max_embedding_concurrency: nextCapacity.max_embedding_concurrency ?? 8, max_qdrant_concurrency: nextCapacity.max_qdrant_concurrency ?? 8 };
      setRuntime(nextRuntime); setOverview(nextOverview); setCapacity(normalizedCapacity); setDraftCapacity(normalizedCapacity); setWorkflow(nextWorkflow); setDraftWorkflow(nextWorkflow);
      setAudioWorkflows(nextAudioWorkflows); setAgents(nextAgents); setRoutes(nextRoutes); setGrpcSelection(nextWorkflow.grpc_audio_workflow_id ?? "");
      const firstAudio = nextAudioWorkflows.find((item) => item.active_revision !== null);
      const firstAgent = nextAgents.find((item) => item.active_revision !== null);
      setRouteTarget((current) => current || (firstAudio ? `audio_workflow:${firstAudio.id}` : firstAgent ? `voice_agent:${firstAgent.id}` : ""));
      setError(null);
    } catch (reason) { setError(errorMessage(reason, "接入与运行状态读取失败。")); }
  }, [api]);
  useEffect(() => { const timer = window.setTimeout(() => void load(), 0); return () => window.clearTimeout(timer); }, [load, refreshVersion]);
  useEffect(() => { const timer = window.setInterval(() => { if (document.visibilityState === "visible") void Promise.all([api.getRuntimeStatus(), api.getRuntimeOverview()]).then(([nextRuntime, nextOverview]) => { setRuntime(nextRuntime); setOverview(nextOverview); }); }, 5000); return () => window.clearInterval(timer); }, [api]);

  const targets = useMemo(() => [
    ...audioWorkflows.filter((item) => item.active_revision !== null).map((item) => ({ value: `audio_workflow:${item.id}`, label: `语音工作流 · ${item.name}`, id: item.id })),
    ...agents.filter((item) => item.active_revision !== null).map((item) => ({ value: `voice_agent:${item.id}`, label: `语音智能体 · ${item.name}`, id: item.id })),
  ], [agents, audioWorkflows]);
  const normalizedRouteModel = routeModel.trim().toLowerCase();
  const routeModelValid = modelPattern.test(normalizedRouteModel);
  const targetLabel = (route: RealtimeRoute) => targets.find((item) => item.id === route.target_id)?.label ?? `${route.target_type} · ${route.target_id}`;

  const bindGrpc = async () => {
    if (!workflow || !grpcSelection) return;
    setSaving(true); setError(null);
    try { await api.setGrpcAudioWorkflow(grpcSelection, workflow.revision); await load(); }
    catch (reason) { setError(errorMessage(reason, "gRPC 默认工作流设置失败。")); }
    finally { setSaving(false); }
  };
  const saveRoute = async (replaceExisting = false) => {
    const [targetType, targetId] = routeTarget.split(":", 2) as ["audio_workflow" | "voice_agent", string];
    if (!routeModelValid || !targetId) return;
    setSaving(true); setError(null);
    try {
      await api.putRealtimeRoute({ model: normalizedRouteModel, target_type: targetType, target_id: targetId, visibility: routeVisibility, replace_existing: replaceExisting });
      setRouteConflict(false); setRouteModel(""); await load();
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === "realtime_route_conflict") setRouteConflict(true);
      else setError(errorMessage(reason, "Realtime 路由保存失败。"));
    } finally { setSaving(false); }
  };
  const deleteRoute = async (model: string) => {
    if (pendingDelete !== model) { setPendingDelete(model); return; }
    setSaving(true);
    try { await api.deleteRealtimeRoute(model); setPendingDelete(null); await load(); }
    catch (reason) { setError(errorMessage(reason, "Realtime 路由删除失败。")); }
    finally { setSaving(false); }
  };
  const saveCapacity = async () => {
    if (!draftCapacity || !capacity) return;
    setSaving(true); setError(null);
    try { const next = await api.updateCapacitySettings({ ...draftCapacity, revision: capacity.revision }); setCapacity(next); setDraftCapacity(next); }
    catch (reason) { setError(errorMessage(reason, "容量设置保存失败。")); }
    finally { setSaving(false); }
  };
  const saveWorkflow = async () => {
    if (!draftWorkflow || !workflow) return;
    setSaving(true); setError(null);
    try { const next = await api.updateWorkflowSettings({ ...draftWorkflow, revision: workflow.revision }); setWorkflow(next); setDraftWorkflow(next); }
    catch (reason) { setError(errorMessage(reason, "流式设置保存失败。")); }
    finally { setSaving(false); }
  };

  if (!runtime || !overview || !draftCapacity || !draftWorkflow) return <main className="workspace-page"><div className="empty-state">正在读取接入与运行状态…</div>{error && <div className="alert alert-error">{error}</div>}</main>;
  const idleLimiter = { active: 0, capacity: 8, rejected: 0 };
  const limiters = [["gRPC 会话", runtime.grpc], ["WSS 会话", runtime.realtime], ["WebRTC 会话", runtime.webrtc], ["ASR", runtime.asr], ["音频向量", runtime.embedding ?? idleLimiter], ["Qdrant", runtime.qdrant ?? idleLimiter], ["Responses", runtime.responses], ["TTS", runtime.tts]] as const;
  const activeAgentAccess = routes.some((route) => route.target_type === "voice_agent" && agents.some((agent) => agent.id === route.target_id && agent.active_revision !== null));
  const bottleneck = activeAgentAccess && Math.max(draftCapacity.realtime.max_sessions, draftCapacity.webrtc.max_sessions) > draftCapacity.max_tts_concurrency * 8;

  return <main className="workspace-page">
    <header className="page-heading"><div><p className="eyebrow">ACCESS & RUNTIME</p><h1>接入与运行</h1><p>先确认协议入口，再调整会话容量和高级流式参数。</p></div><button className="secondary-button" onClick={() => void load()}>刷新</button></header>
    {error && <div className="alert alert-error"><span>{error}</span><button className="text-button" onClick={() => setError(null)}>关闭</button></div>}
    <section id="access" className="access-overview"><article className="status-card"><div><span className={`health-dot ${overview.control_plane === "ready" ? "healthy" : "error"}`} /><strong>管理服务</strong></div><h2>{overview.control_plane === "ready" ? "在线" : "不可用"}</h2><p>数据库与控制面 API</p></article><article className="status-card"><div><span className={`health-dot ${overview.grpc.state === "ready" ? "healthy" : "warning"}`} /><strong>gRPC</strong></div><h2>{overview.grpc.state === "ready" ? `工作流 v${overview.grpc.workflow_revision}` : "待配置"}</h2><p>UxSpeech.StreamingRecognize</p></article><article className="status-card"><div><span className={`health-dot ${overview.realtime.state === "ready" ? "healthy" : "warning"}`} /><strong>Realtime</strong></div><h2>{overview.realtime.public_routes} 个公开 model</h2><p>WSS 与 WebRTC 共用路由</p></article></section>
    {overview.issues.length > 0 && <div className="issue-strip">{overview.issues.map((issue) => <div key={`${issue.code}-${issue.resource_id ?? "global"}`}><strong>{issue.scope === "grpc" ? "gRPC" : issue.scope === "realtime" ? "Realtime" : "控制面"}</strong><span>{issue.message}</span></div>)}</div>}

    <div className="access-columns">
      <section className="panel"><div className="panel-heading"><div><h2>gRPC 默认工作流</h2><p>gRPC 请求不携带 model，因此只绑定一个活动工作流。</p></div><span className={`status-chip ${overview.grpc.state === "ready" ? "status-published_healthy" : "status-unpublished"}`}>{overview.grpc.state === "ready" ? "已接入" : "待配置"}</span></div><div className="inline-setting"><select value={grpcSelection} onChange={(event) => setGrpcSelection(event.target.value)}><option value="">请选择已发布工作流</option>{audioWorkflows.filter((item) => item.active_revision !== null).map((item) => <option key={item.id} value={item.id}>{item.name} · v{item.active_revision}</option>)}</select><button className="primary-button" disabled={saving || !grpcSelection || grpcSelection === workflow?.grpc_audio_workflow_id} onClick={() => void bindGrpc()}>设为默认</button></div></section>
      <section className="panel"><div className="panel-heading"><div><h2>新建 Realtime 路由</h2><p>model 冲突时不会静默替换旧目标。</p></div></div><div className="route-manager-form"><label><span>Model</span><input value={routeModel} onChange={(event) => { setRouteModel(event.target.value); setRouteConflict(false); }} placeholder="assistant" /></label><label><span>目标</span><select value={routeTarget} onChange={(event) => setRouteTarget(event.target.value)}>{targets.map((target) => <option key={target.value} value={target.value}>{target.label}</option>)}</select></label><label><span>可见性</span><select value={routeVisibility} onChange={(event) => setRouteVisibility(event.target.value as "public" | "internal")}><option value="public">Public</option><option value="internal">Internal</option></select></label><button className="primary-button" disabled={saving || !routeModelValid || !routeTarget} onClick={() => void saveRoute(false)}>创建路由</button></div>{routeModel && !routeModelValid && <div className="inline-error">Model 仅支持小写字母、数字、点、下划线或连字符。</div>}{routeConflict && <div className="alert alert-warning"><div><strong>Model 已被其他资源占用</strong><p>只有明确确认后才会替换旧目标。</p></div><div className="inline-actions"><button className="secondary-button" onClick={() => setRouteConflict(false)}>取消</button><button className="danger-button" disabled={saving} onClick={() => void saveRoute(true)}>确认替换</button></div></div>}</section>
    </div>
    <section className="panel route-manager"><div className="panel-heading"><div><h2>Realtime 模型路由</h2><p>{routes.length} 个稳定入口</p></div></div>{routes.length === 0 ? <div className="empty-state">尚未创建路由；已发布资源不会自动暴露。</div> : <div className="route-table"><div className="route-table-row table-header"><span>Model</span><span>目标</span><span>可见性</span><span>操作</span></div>{routes.map((route) => <div className="route-table-row" key={route.model}><code>{route.model}</code><span>{targetLabel(route)}</span><span className="status-chip">{route.visibility === "public" ? "Public" : "Internal"}</span><button className={pendingDelete === route.model ? "danger-button" : "text-button"} disabled={saving} onClick={() => void deleteRoute(route.model)}>{pendingDelete === route.model ? "再次点击确认" : "移除"}</button></div>)}</div>}</section>

    <section className="runtime-section"><div className="section-heading"><div><p className="eyebrow">CAPACITY</p><h2>容量与利用率</h2></div><button className="primary-button" disabled={saving} onClick={() => void saveCapacity()}>应用容量设置</button></div><div className="runtime-metrics">{limiters.map(([label, value]) => { const usage = value.capacity ? Math.round(value.active / value.capacity * 100) : 0; return <article className="metric-card runtime-metric" key={label}><div><span>{label}</span><strong>{value.active} / {value.capacity}</strong></div><div className="utilization-track"><i style={{ width: `${Math.min(100, usage)}%` }} /></div><small>{usage}% 使用 · 累计拒绝 {value.rejected}</small></article>; })}</div>{bottleneck && <div className="alert alert-warning"><div><strong>TTS 可能成为容量瓶颈</strong><p>已有活动语音智能体接入，而会话上限明显高于 TTS 并发。</p></div></div>}<section className="panel"><div className="settings-grid three-columns">{[["gRPC 最大会话", draftCapacity.grpc.max_sessions, (value: number) => setDraftCapacity({ ...draftCapacity, grpc: { max_sessions: value } })], ["WSS 最大会话", draftCapacity.realtime.max_sessions, (value: number) => setDraftCapacity({ ...draftCapacity, realtime: { max_sessions: value } })], ["WebRTC 最大会话", draftCapacity.webrtc.max_sessions, (value: number) => setDraftCapacity({ ...draftCapacity, webrtc: { max_sessions: value } })], ["ASR 并发", draftCapacity.max_asr_concurrency, (value: number) => setDraftCapacity({ ...draftCapacity, max_asr_concurrency: value })], ["音频向量并发", draftCapacity.max_embedding_concurrency, (value: number) => setDraftCapacity({ ...draftCapacity, max_embedding_concurrency: value })], ["Qdrant 并发", draftCapacity.max_qdrant_concurrency, (value: number) => setDraftCapacity({ ...draftCapacity, max_qdrant_concurrency: value })], ["Responses 并发", draftCapacity.max_responses_concurrency, (value: number) => setDraftCapacity({ ...draftCapacity, max_responses_concurrency: value })], ["TTS 并发", draftCapacity.max_tts_concurrency, (value: number) => setDraftCapacity({ ...draftCapacity, max_tts_concurrency: value })]].map(([label, value, update]) => <label key={String(label)}><span>{label as string}</span><input type="number" min="1" value={value as number} onChange={(event) => (update as (value: number) => void)(Number(event.target.value))} /></label>)}</div></section></section>

    <details className="panel advanced-settings"><summary><div><p className="eyebrow">ADVANCED</p><strong>流式队列与语言</strong><small>仅在确认背压或延迟问题后调整</small></div><span>展开</span></summary><div className="settings-grid three-columns">{[["输入重分块（字节）", draftWorkflow.min_chunk_bytes, "min_chunk_bytes"], ["输入音频队列", draftWorkflow.audio_queue_capacity, "audio_queue_capacity"], ["片段队列", draftWorkflow.segment_queue_capacity, "segment_queue_capacity"], ["结果队列", draftWorkflow.result_queue_capacity, "result_queue_capacity"]].map(([label, value, key]) => <label key={String(key)}><span>{label}</span><input type="number" min="1" value={value} onChange={(event) => setDraftWorkflow({ ...draftWorkflow, [key]: Number(event.target.value) })} /></label>)}<label><span>默认语言</span><input value={draftWorkflow.default_language_code} onChange={(event) => setDraftWorkflow({ ...draftWorkflow, default_language_code: event.target.value })} /></label></div><div className="panel-actions"><button className="primary-button" disabled={saving} onClick={() => void saveWorkflow()}>应用高级设置</button></div></details>
  </main>;
}
