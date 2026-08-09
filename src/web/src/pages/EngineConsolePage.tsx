import * as AlertDialog from "@radix-ui/react-alert-dialog";
import * as Dialog from "@radix-ui/react-dialog";
import { useCallback, useEffect, useMemo, useState } from "react";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi, EngineApi, EngineAsset, EngineKind, EngineProtocol, EngineWritePayload } from "../api/types";

interface Props { api: AudioHarnessApi; refreshVersion: number; }

const kindMeta: Record<EngineKind, { label: string; api: EngineApi; protocol: EngineProtocol }> = {
  vad: { label: "VAD", api: "ux_vad", protocol: "grpc" }, asr: { label: "ASR", api: "ux_speech", protocol: "grpc" },
  llm: { label: "Responses", api: "responses", protocol: "https" }, tts: { label: "TTS", api: "qwen_tts_duplex", protocol: "grpc" },
};

function blank(kind: EngineKind = "asr"): EngineWritePayload {
  const meta = kindMeta[kind];
  return { slug: "", name: "", kind, api: meta.api, protocol: meta.protocol, target: "", connect_timeout_seconds: 10,
    request_timeout_seconds: kind === "llm" ? 180 : null, api_key: kind === "llm" ? "" : null, credential_env: null, enabled: true,
    status: "unknown", image: null, description: null, status_basis: null, status_observed_at: null, source: null, details: {} };
}

function statusLabel(status: string): string {
  return ({ healthy: "健康", unhealthy: "不可用", unknown: "未探测", checking: "探测中", stale: "结果过期", disabled: "已停用" } as Record<string, string>)[status] ?? status;
}

function healthGroup(item: EngineAsset): "healthy" | "unhealthy" | "unknown" | "disabled" {
  if (item.status === "healthy") return "healthy";
  if (item.status === "disabled" || !item.enabled) return "disabled";
  if (item.status === "unknown" || item.status === "checking") return "unknown";
  return "unhealthy";
}

export function EngineConsolePage({ api, refreshVersion }: Props) {
  const [items, setItems] = useState<EngineAsset[]>([]);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<EngineKind | "all">("all");
  const [health, setHealth] = useState<"all" | "healthy" | "unhealthy" | "unknown" | "disabled">("all");
  const [editing, setEditing] = useState<EngineAsset | "new" | null>(null);
  const [deleting, setDeleting] = useState<EngineAsset | null>(null);
  const [values, setValues] = useState<EngineWritePayload>(blank());
  const [usage, setUsage] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setItems((await api.listEngines({ limit: 200 })).items); }
    catch (reason) { setError(errorMessage(reason, "引擎列表读取失败。")); }
  }, [api]);
  useEffect(() => { const timer = window.setTimeout(() => void load(), 0); return () => window.clearTimeout(timer); }, [load, refreshVersion]);
  const filtered = useMemo(() => items.filter((item) => (kind === "all" || item.kind === kind) && (health === "all" || healthGroup(item) === health) && (!query || `${item.name} ${item.slug} ${item.target}`.toLowerCase().includes(query.toLowerCase()))), [health, items, kind, query]);

  const closeEditor = () => { if (!busy) setEditing(null); };
  const openCreate = (nextKind: EngineKind = "asr") => { setEditing("new"); setValues(blank(nextKind)); setUsage([]); setError(null); };
  const openEdit = async (engine: EngineAsset) => {
    setEditing(engine); setValues({ slug: engine.slug, name: engine.name, kind: engine.kind, api: engine.api, protocol: engine.protocol,
      target: engine.target, connect_timeout_seconds: engine.connect_timeout_seconds, request_timeout_seconds: engine.request_timeout_seconds,
      credential_env: engine.credential_env ?? null, api_key: engine.api_key ?? null, enabled: engine.enabled, status: engine.status,
      image: engine.image, description: engine.description, status_basis: engine.status_basis, status_observed_at: engine.status_observed_at, source: engine.source, details: engine.details });
    try { setUsage((await api.getEngineUsage(engine.id)).references ?? []); }
    catch (reason) { setError(errorMessage(reason, "引擎引用读取失败。")); }
  };
  const changeKind = (nextKind: EngineKind) => { const meta = kindMeta[nextKind]; setValues((current) => ({ ...current, kind: nextKind, api: meta.api, protocol: meta.protocol, api_key: nextKind === "llm" ? current.api_key ?? "" : null, credential_env: null, request_timeout_seconds: nextKind === "llm" ? current.request_timeout_seconds ?? 180 : null })); };
  const save = async () => { setBusy(true); setError(null); try { if (editing === "new") await api.createEngine(values); else if (editing) await api.updateEngine(editing.id, values); setEditing(null); await load(); } catch (reason) { setError(errorMessage(reason, "引擎保存失败。")); } finally { setBusy(false); } };
  const probe = async (engine: EngineAsset) => { if (!api.probeEngine) return; setBusy(true); setError(null); try { await api.probeEngine(engine.id); await load(); } catch (reason) { setError(errorMessage(reason, "引擎探测失败。")); } finally { setBusy(false); } };
  const remove = async () => { if (!deleting) return; try { await api.deleteEngine(deleting.id); setDeleting(null); await load(); } catch (reason) { setError(errorMessage(reason, "引擎仍被工作流引用，无法删除。")); setDeleting(null); } };

  return <main className="workspace-page">
    <header className="page-heading"><div><p className="eyebrow">ENGINES</p><h1>推理引擎</h1><p>连接信息、真实健康与工作流引用集中管理。</p></div><button className="primary-button" onClick={() => openCreate()}>注册引擎</button></header>
    {error && <div className="alert alert-error" role="alert">{error}<button className="text-button" onClick={() => setError(null)}>关闭</button></div>}
    <section className="engine-health-summary" aria-label="引擎健康筛选">{(["all", "healthy", "unhealthy", "unknown", "disabled"] as const).map((value) => <button className={health === value ? "active" : ""} key={value} onClick={() => setHealth(value)}><span>{({ all: "全部", healthy: "健康", unhealthy: "异常", unknown: "未探测", disabled: "已停用" } as const)[value]}</span><strong>{value === "all" ? items.length : items.filter((item) => healthGroup(item) === value).length}</strong></button>)}</section>
    <section className="panel engine-toolbar" aria-label="引擎筛选"><div className="filter-tabs">{(["all", "vad", "asr", "llm", "tts"] as const).map((value) => <button className={kind === value ? "active" : ""} key={value} onClick={() => setKind(value)}>{value === "all" ? "全部能力" : kindMeta[value].label} <span>{value === "all" ? items.length : items.filter((item) => item.kind === value).length}</span></button>)}</div><input className="search-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、Slug 或地址" aria-label="搜索引擎" /></section>
    <section className="panel engine-table-panel" aria-label="引擎列表"><div className="engine-table table-header"><span>引擎</span><span>接口 / 地址</span><span>健康</span><span>最近探测</span><span>操作</span></div>{filtered.length === 0 ? <div className="empty-state">没有符合条件的引擎</div> : filtered.map((engine) => <div className="engine-table" key={engine.id}><span><strong>{engine.name}</strong><small>{engine.slug} · {kindMeta[engine.kind]?.label ?? engine.kind}</small></span><span><code>{engine.api}</code><small>{engine.target}</small></span><span><span className={`status-chip engine-${engine.status}`}>{statusLabel(engine.status)}</span>{engine.status_basis && <small className="health-reason" title={engine.status_basis}>{engine.status_basis}</small>}</span><span>{engine.status_observed_at ? new Date(engine.status_observed_at).toLocaleString("zh-CN") : "尚未探测"}{engine.probe_latency_ms != null && <small>{Math.round(engine.probe_latency_ms)} ms</small>}</span><span className="row-actions"><button onClick={() => void probe(engine)} disabled={busy || !engine.enabled}>复检</button><button onClick={() => void openEdit(engine)}>编辑</button><button className="danger-text" onClick={() => setDeleting(engine)}>删除</button></span></div>)}</section>

    <Dialog.Root open={Boolean(editing)} onOpenChange={(open) => { if (!open) closeEditor(); }}><Dialog.Portal><Dialog.Overlay className="drawer-backdrop" /><Dialog.Content className="control-drawer" aria-describedby={undefined}><header><div><p className="eyebrow">ENGINE</p><Dialog.Title>{editing === "new" ? "注册引擎" : `编辑 ${editing?.name ?? ""}`}</Dialog.Title></div><Dialog.Close className="icon-button" disabled={busy} aria-label="关闭编辑引擎">×</Dialog.Close></header><div className="drawer-body"><div className="form-grid two-columns"><label><span>能力类型</span><select disabled={editing !== "new"} value={values.kind} onChange={(event) => changeKind(event.target.value as EngineKind)}>{(["vad", "asr", "llm", "tts"] as EngineKind[]).map((value) => <option key={value} value={value}>{kindMeta[value].label}</option>)}</select></label><label><span>名称</span><input value={values.name} onChange={(event) => setValues({ ...values, name: event.target.value })} /></label><label><span>Slug</span><input value={values.slug} onChange={(event) => setValues({ ...values, slug: event.target.value.toLowerCase() })} /></label><label><span>接口</span><input value={`${values.protocol.toUpperCase()} · ${values.api}`} disabled /></label><label className="full-span"><span>{values.kind === "llm" ? "API Base URL" : "gRPC 地址"}</span><input value={values.target} onChange={(event) => setValues({ ...values, target: event.target.value })} placeholder={values.kind === "llm" ? "https://api.example.com" : "127.0.0.1:50051"} /></label>{values.kind === "llm" && <label className="full-span"><span>模型 API Key（内网明文存储与展示）</span><input value={values.api_key ?? ""} onChange={(event) => setValues({ ...values, api_key: event.target.value })} autoComplete="off" /></label>}<label><span>连接超时（秒）</span><input type="number" min="0.1" value={values.connect_timeout_seconds} onChange={(event) => setValues({ ...values, connect_timeout_seconds: Number(event.target.value) })} /></label><label><span>请求超时（秒）</span><input type="number" min="0.1" value={values.request_timeout_seconds ?? ""} onChange={(event) => setValues({ ...values, request_timeout_seconds: event.target.value ? Number(event.target.value) : null })} /></label><label className="full-span"><span>描述</span><textarea value={values.description ?? ""} onChange={(event) => setValues({ ...values, description: event.target.value || null })} /></label><label className="switch-control full-span"><input type="checkbox" checked={values.enabled} onChange={(event) => setValues({ ...values, enabled: event.target.checked })} /><span>启用并参与后台健康探测</span></label></div>{editing !== "new" && <section className="usage-box"><h3>工作流引用</h3>{usage.length ? usage.map((item) => <span key={item}>{item}</span>) : <p>当前没有工作流引用该引擎。</p>}</section>}</div><footer><Dialog.Close className="secondary-button" disabled={busy}>取消</Dialog.Close><button className="primary-button" disabled={busy || !values.name || !values.slug || !values.target || (values.kind === "llm" && !values.api_key)} onClick={() => void save()}>{busy ? "保存中…" : "保存引擎"}</button></footer></Dialog.Content></Dialog.Portal></Dialog.Root>
    <AlertDialog.Root open={Boolean(deleting)} onOpenChange={(open) => { if (!open) setDeleting(null); }}><AlertDialog.Portal><AlertDialog.Overlay className="drawer-backdrop" /><AlertDialog.Content className="confirm-dialog"><AlertDialog.Title>删除引擎？</AlertDialog.Title><AlertDialog.Description>将删除“{deleting?.name}”。如果该引擎仍被工作流引用，服务会拒绝该操作。</AlertDialog.Description><div className="panel-actions"><AlertDialog.Cancel className="secondary-button">取消</AlertDialog.Cancel><AlertDialog.Action className="primary-button" onClick={() => void remove()}>确认删除</AlertDialog.Action></div></AlertDialog.Content></AlertDialog.Portal></AlertDialog.Root>
  </main>;
}
