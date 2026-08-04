import { useCallback, useEffect, useState } from "react";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi, CapacitySettings, RuntimeStatus, WorkflowSettings } from "../api/types";

interface Props { api: AudioHarnessApi; refreshVersion: number; }

export function RuntimePage({ api, refreshVersion }: Props) {
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [capacity, setCapacity] = useState<CapacitySettings | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowSettings | null>(null);
  const [draftCapacity, setDraftCapacity] = useState<CapacitySettings | null>(null);
  const [draftWorkflow, setDraftWorkflow] = useState<WorkflowSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!api.getRuntimeStatus) return;
    try {
      const [nextRuntime, nextCapacity, nextWorkflow] = await Promise.all([api.getRuntimeStatus(), api.getCapacitySettings(), api.getWorkflowSettings()]);
      setRuntime(nextRuntime); setCapacity(nextCapacity); setDraftCapacity(nextCapacity); setWorkflow(nextWorkflow); setDraftWorkflow(nextWorkflow);
    } catch (reason) { setError(errorMessage(reason, "运行状态读取失败。")); }
  }, [api]);

  useEffect(() => { const timer = window.setTimeout(() => void load(), 0); return () => window.clearTimeout(timer); }, [load, refreshVersion]);
  useEffect(() => { const timer = window.setInterval(() => { if (document.visibilityState === "visible" && api.getRuntimeStatus) void api.getRuntimeStatus().then(setRuntime); }, 5000); return () => window.clearInterval(timer); }, [api]);

  const save = async () => {
    if (!draftCapacity || !draftWorkflow || !capacity || !workflow) return;
    setSaving(true); setError(null);
    try {
      const [nextCapacity, nextWorkflow] = await Promise.all([
        api.updateCapacitySettings({ ...draftCapacity, revision: capacity.revision }),
        api.updateWorkflowSettings({ ...draftWorkflow, revision: workflow.revision }),
      ]);
      setCapacity(nextCapacity); setDraftCapacity(nextCapacity); setWorkflow(nextWorkflow); setDraftWorkflow(nextWorkflow);
    } catch (reason) { setError(errorMessage(reason, "运行设置保存失败，可能已被其他页面修改。")); }
    finally { setSaving(false); }
  };

  if (!runtime || !draftCapacity || !draftWorkflow) return <main className="workspace-page"><div className="empty-state">正在读取运行状态…</div>{error && <div className="alert alert-error">{error}</div>}</main>;
  const limiters = [["gRPC 会话", runtime.grpc], ["WSS 会话", runtime.realtime], ["WebRTC 会话", runtime.webrtc], ["ASR", runtime.asr], ["Responses", runtime.responses], ["TTS", runtime.tts]] as const;
  const bottleneck = Math.max(draftCapacity.grpc.max_sessions, draftCapacity.realtime.max_sessions, draftCapacity.webrtc.max_sessions) > (draftCapacity.max_tts_concurrency ?? 1) * 8;

  return <main className="workspace-page">
    <header className="page-heading"><div><p className="eyebrow">RUNTIME</p><h1>运行状态与容量</h1><p>当前利用率来自进程内计数，服务重启后清零。</p></div><button className="secondary-button" onClick={() => void load()}>刷新</button></header>
    {error && <div className="alert alert-error">{error}</div>}
    <section className="runtime-metrics">{limiters.map(([label, value]) => { const usage = value.capacity ? Math.round(value.active / value.capacity * 100) : 0; return <article className="metric-card runtime-metric" key={label}><div><span>{label}</span><strong>{value.active} / {value.capacity}</strong></div><div className="utilization-track"><i style={{ width: `${Math.min(100, usage)}%` }} /></div><small>{usage}% 使用 · 累计拒绝 {value.rejected}</small></article>; })}</section>
    {bottleneck && <div className="alert alert-warning"><div><strong>TTS 可能成为容量瓶颈</strong><p>会话上限明显高于 TTS 并发。Voice Agent 高峰期可能因并发保护被立即拒绝。</p></div></div>}
    <div className="settings-columns">
      <section className="panel"><div className="panel-heading"><div><h2>协议与推理容量</h2><p>修改只影响新会话；当前会话继续使用原快照。</p></div></div><div className="settings-grid">{[["gRPC 最大会话", draftCapacity.grpc.max_sessions, (value: number) => setDraftCapacity({ ...draftCapacity, grpc: { max_sessions: value } })], ["WSS 最大会话", draftCapacity.realtime.max_sessions, (value: number) => setDraftCapacity({ ...draftCapacity, realtime: { max_sessions: value } })], ["WebRTC 最大会话", draftCapacity.webrtc.max_sessions, (value: number) => setDraftCapacity({ ...draftCapacity, webrtc: { max_sessions: value } })], ["ASR 并发", draftCapacity.max_asr_concurrency, (value: number) => setDraftCapacity({ ...draftCapacity, max_asr_concurrency: value })], ["Responses 并发", draftCapacity.max_responses_concurrency ?? 8, (value: number) => setDraftCapacity({ ...draftCapacity, max_responses_concurrency: value })], ["TTS 并发", draftCapacity.max_tts_concurrency ?? 1, (value: number) => setDraftCapacity({ ...draftCapacity, max_tts_concurrency: value })]].map(([label, value, update]) => <label key={String(label)}><span>{label as string}</span><input type="number" min="1" value={value as number} onChange={(event) => (update as (value: number) => void)(Number(event.target.value))} /></label>)}</div></section>
      <section className="panel"><div className="panel-heading"><div><h2>流式队列</h2><p>队列越大越能吸收抖动，但会增加最坏延迟与内存占用。</p></div></div><div className="settings-grid">{[["输入重分块（字节）", draftWorkflow.min_chunk_bytes, "min_chunk_bytes"], ["输入音频队列", draftWorkflow.audio_queue_capacity, "audio_queue_capacity"], ["片段队列", draftWorkflow.segment_queue_capacity, "segment_queue_capacity"], ["结果队列", draftWorkflow.result_queue_capacity, "result_queue_capacity"]].map(([label, value, key]) => <label key={String(key)}><span>{label}</span><input type="number" min="1" value={value} onChange={(event) => setDraftWorkflow({ ...draftWorkflow, [key]: Number(event.target.value) })} /></label>)}<label><span>默认语言</span><input value={draftWorkflow.default_language_code} onChange={(event) => setDraftWorkflow({ ...draftWorkflow, default_language_code: event.target.value })} /></label></div></section>
    </div>
    <div className="sticky-save-bar"><div><strong>应用运行设置</strong><span>保存前请确认容量与下游引擎实际承载能力匹配。</span></div><button className="primary-button" disabled={saving} onClick={() => void save()}>{saving ? "正在应用…" : "应用到新会话"}</button></div>
  </main>;
}
