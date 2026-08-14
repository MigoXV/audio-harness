import * as AlertDialog from "@radix-ui/react-alert-dialog";
import * as Dialog from "@radix-ui/react-dialog";
import { useCallback, useEffect, useMemo, useState } from "react";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi, EngineApi, EngineAsset, EngineKind, EngineProtocol, EngineWritePayload } from "../api/types";

interface Props { api: AudioHarnessApi; refreshVersion: number; }

const kindMeta: Record<EngineKind, { label: string; api: EngineApi; protocol: EngineProtocol }> = {
  vad: { label: "VAD", api: "ux_vad", protocol: "grpc" }, asr: { label: "ASR", api: "ux_speech", protocol: "grpc" },
  llm: { label: "Responses", api: "responses", protocol: "https" }, tts: { label: "TTS", api: "qwen_tts_duplex", protocol: "grpc" },
  embedding: { label: "音频向量", api: "engine_feature", protocol: "grpc" },
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
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set());
  const [pendingDisable, setPendingDisable] = useState<{ engine: EngineAsset; references: string[] } | null>(null);
  const [values, setValues] = useState<EngineWritePayload>(blank());
  const [usage, setUsage] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems((await api.listEngines({ limit: 200 })).items);
      setError(null);
    }
    catch (reason) { setError(errorMessage(reason, "引擎列表读取失败。")); }
    finally { setLoading(false); }
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
  const toggleExpanded = (engineId: string) => setExpandedIds((current) => {
    const next = new Set(current);
    if (next.has(engineId)) next.delete(engineId); else next.add(engineId);
    return next;
  });
  const requestDisable = async (engine: EngineAsset) => {
    setBusy(true); setError(null);
    try {
      const references = (await api.getEngineUsage(engine.id)).references ?? [];
      setPendingDisable({ engine, references });
    } catch (reason) { setError(errorMessage(reason, "引擎引用读取失败。")); }
    finally { setBusy(false); }
  };
  const disable = async () => {
    if (!pendingDisable || pendingDisable.references.length > 0) return;
    setBusy(true); setError(null);
    try { await api.updateEngine(pendingDisable.engine.id, { enabled: false }); setPendingDisable(null); await load(); }
    catch (reason) { setError(errorMessage(reason, "引擎停用失败。")); setPendingDisable(null); }
    finally { setBusy(false); }
  };
  const enable = async (engine: EngineAsset) => {
    setBusy(true); setError(null);
    try { await api.updateEngine(engine.id, { enabled: true }); await load(); }
    catch (reason) { setError(errorMessage(reason, "引擎启用失败。")); }
    finally { setBusy(false); }
  };
  const remove = async () => { if (!deleting) return; try { await api.deleteEngine(deleting.id); setDeleting(null); await load(); } catch (reason) { setError(errorMessage(reason, "引擎仍被工作流引用，无法删除。")); setDeleting(null); } };

  return <main className="workspace-page">
    <header className="page-heading"><div><p className="eyebrow">ENGINES</p><h1>推理引擎</h1><p>连接信息、真实健康与工作流引用集中管理。</p></div><button className="primary-button" onClick={() => openCreate()}>注册引擎</button></header>
    {error && <div className="alert alert-error" role="alert">{error}<button className="text-button" onClick={() => setError(null)}>关闭</button></div>}
    <section className="engine-health-summary" aria-label="引擎健康筛选">{(["all", "healthy", "unhealthy", "unknown", "disabled"] as const).map((value) => <button className={health === value ? "active" : ""} key={value} onClick={() => setHealth(value)}><span>{({ all: "全部", healthy: "健康", unhealthy: "异常", unknown: "未探测", disabled: "已停用" } as const)[value]}</span><strong>{value === "all" ? items.length : items.filter((item) => healthGroup(item) === value).length}</strong></button>)}</section>
    <section className="panel engine-toolbar" aria-label="引擎筛选"><div className="filter-tabs">{(["all", "vad", "asr", "embedding", "llm", "tts"] as const).map((value) => <button className={kind === value ? "active" : ""} key={value} onClick={() => setKind(value)}>{value === "all" ? "全部能力" : kindMeta[value].label} <span>{value === "all" ? items.length : items.filter((item) => item.kind === value).length}</span></button>)}</div><input className="search-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、Slug 或地址" aria-label="搜索引擎" /></section>
    <section className="panel engine-accordion-list" aria-label="引擎列表">{loading && items.length === 0 ? <div className="empty-state" role="status" aria-live="polite">正在加载引擎…</div> : error && items.length === 0 ? <div className="empty-state"><strong>引擎列表暂时无法读取</strong><button className="secondary-button" onClick={() => void load()}>重新加载</button></div> : items.length === 0 ? <div className="empty-state">尚未注册引擎</div> : filtered.length === 0 ? <div className="empty-state">没有符合条件的引擎</div> : filtered.map((engine) => { const expanded = expandedIds.has(engine.id); const detailsId = `engine-details-${engine.id}`; return <article className={`engine-card ${expanded ? "expanded" : "collapsed"}`} key={engine.id}>
      <header className="engine-card-summary">
        <button className="engine-card-toggle" aria-expanded={expanded} aria-controls={detailsId} onClick={() => toggleExpanded(engine.id)}>
          <span><strong>{engine.name}</strong><small>{engine.slug} · {kindMeta[engine.kind]?.label ?? engine.kind}</small></span>
          <code>{engine.target}</code>
          <span className={`status-chip engine-${engine.status}`}>{statusLabel(engine.status)}</span>
          <i aria-hidden="true">⌄</i>
        </button>
        <button className={engine.enabled ? "danger-text engine-lifecycle-button" : "secondary-button engine-lifecycle-button"} disabled={busy} onClick={() => void (engine.enabled ? requestDisable(engine) : enable(engine))}>{engine.enabled ? "停用" : "启用"}</button>
      </header>
      {expanded && <div className="engine-card-details" id={detailsId}>
        <dl><div><dt>接口</dt><dd><code>{engine.protocol.toUpperCase()} · {engine.api}</code></dd></div><div><dt>最近探测</dt><dd>{engine.status_observed_at ? new Date(engine.status_observed_at).toLocaleString("zh-CN") : "尚未探测"}</dd></div><div><dt>探测延迟</dt><dd>{engine.probe_latency_ms != null ? `${Math.round(engine.probe_latency_ms)} ms` : "—"}</dd></div><div><dt>修订</dt><dd>r{engine.revision}</dd></div></dl>
        {engine.status_basis && <p className="engine-status-reason"><strong>状态说明</strong>{engine.status_basis}</p>}
        {engine.description && <p className="engine-description">{engine.description}</p>}
        <div className="row-actions"><button onClick={() => void probe(engine)} disabled={busy || !engine.enabled}>复检</button><button onClick={() => void openEdit(engine)}>编辑</button><button className="danger-text" onClick={() => setDeleting(engine)}>删除</button></div>
      </div>}
    </article>; })}</section>

    <Dialog.Root open={Boolean(editing)} onOpenChange={(open) => { if (!open) closeEditor(); }}><Dialog.Portal><Dialog.Overlay className="drawer-backdrop" /><Dialog.Content className="control-drawer" aria-describedby={undefined}><header><div><p className="eyebrow">ENGINE</p><Dialog.Title>{editing === "new" ? "注册引擎" : `编辑 ${editing?.name ?? ""}`}</Dialog.Title></div><Dialog.Close className="icon-button" disabled={busy} aria-label="关闭编辑引擎">×</Dialog.Close></header><div className="drawer-body"><div className="form-grid two-columns"><label><span>能力类型</span><select disabled={editing !== "new"} value={values.kind} onChange={(event) => changeKind(event.target.value as EngineKind)}>{(["vad", "asr", "embedding", "llm", "tts"] as EngineKind[]).map((value) => <option key={value} value={value}>{kindMeta[value].label}</option>)}</select></label><label><span>名称</span><input value={values.name} onChange={(event) => setValues({ ...values, name: event.target.value })} /></label><label><span>Slug</span><input value={values.slug} onChange={(event) => setValues({ ...values, slug: event.target.value.toLowerCase() })} /></label><label><span>接口</span><input value={`${values.protocol.toUpperCase()} · ${values.api}`} disabled /></label><label className="full-span"><span>{values.kind === "llm" ? "API Base URL" : "gRPC 地址"}</span><input value={values.target} onChange={(event) => setValues({ ...values, target: event.target.value })} placeholder={values.kind === "llm" ? "https://api.example.com" : "127.0.0.1:50051"} /></label>{values.kind === "llm" && <label className="full-span"><span>模型 API Key（内网明文存储与展示）</span><input value={values.api_key ?? ""} onChange={(event) => setValues({ ...values, api_key: event.target.value })} autoComplete="off" /></label>}<label><span>连接超时（秒）</span><input type="number" min="0.1" value={values.connect_timeout_seconds} onChange={(event) => setValues({ ...values, connect_timeout_seconds: Number(event.target.value) })} /></label><label><span>请求超时（秒）</span><input type="number" min="0.1" value={values.request_timeout_seconds ?? ""} onChange={(event) => setValues({ ...values, request_timeout_seconds: event.target.value ? Number(event.target.value) : null })} /></label><label className="full-span"><span>描述</span><textarea value={values.description ?? ""} onChange={(event) => setValues({ ...values, description: event.target.value || null })} /></label><label className="switch-control full-span"><input type="checkbox" checked={values.enabled} onChange={(event) => setValues({ ...values, enabled: event.target.checked })} /><span>启用并参与后台健康探测</span></label></div>{editing !== "new" && <section className="usage-box"><h3>工作流引用</h3>{usage.length ? usage.map((item) => <span key={item}>{item}</span>) : <p>当前没有工作流引用该引擎。</p>}</section>}</div><footer><Dialog.Close className="secondary-button" disabled={busy}>取消</Dialog.Close><button className="primary-button" disabled={busy || !values.name || !values.slug || !values.target || (values.kind === "llm" && !values.api_key)} onClick={() => void save()}>{busy ? "保存中…" : "保存引擎"}</button></footer></Dialog.Content></Dialog.Portal></Dialog.Root>
    <AlertDialog.Root open={Boolean(deleting)} onOpenChange={(open) => { if (!open) setDeleting(null); }}><AlertDialog.Portal><AlertDialog.Overlay className="drawer-backdrop" /><AlertDialog.Content className="confirm-dialog"><AlertDialog.Title>删除引擎？</AlertDialog.Title><AlertDialog.Description>将删除“{deleting?.name}”。如果该引擎仍被工作流引用，服务会拒绝该操作。</AlertDialog.Description><div className="panel-actions"><AlertDialog.Cancel className="secondary-button">取消</AlertDialog.Cancel><AlertDialog.Action className="primary-button" onClick={() => void remove()}>确认删除</AlertDialog.Action></div></AlertDialog.Content></AlertDialog.Portal></AlertDialog.Root>
    <AlertDialog.Root open={Boolean(pendingDisable)} onOpenChange={(open) => { if (!open) setPendingDisable(null); }}><AlertDialog.Portal><AlertDialog.Overlay className="drawer-backdrop" /><AlertDialog.Content className="confirm-dialog"><AlertDialog.Title>{pendingDisable?.references.length ? "当前不能停用引擎" : "停用引擎？"}</AlertDialog.Title><AlertDialog.Description>{pendingDisable?.references.length ? `“${pendingDisable.engine.name}”仍被以下处理链引用，请先解除引用：` : `停用“${pendingDisable?.engine.name ?? ""}”后将停止健康探测，也不能再绑定到新配置。`}</AlertDialog.Description>{Boolean(pendingDisable?.references.length) && <ul className="engine-reference-list">{pendingDisable?.references.map((reference) => <li key={reference}>{reference}</li>)}</ul>}<div className="panel-actions"><AlertDialog.Cancel className="secondary-button">{pendingDisable?.references.length ? "知道了" : "取消"}</AlertDialog.Cancel>{pendingDisable?.references.length === 0 && <AlertDialog.Action className="danger-button" disabled={busy} onClick={() => void disable()}>确认停用</AlertDialog.Action>}</div></AlertDialog.Content></AlertDialog.Portal></AlertDialog.Root>
  </main>;
}
