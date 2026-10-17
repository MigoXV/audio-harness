import * as AlertDialog from "@radix-ui/react-alert-dialog";
import * as Tabs from "@radix-ui/react-tabs";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { AudioHarnessApi, EngineAsset } from "../../api/types";
import { ApiError, errorMessage } from "../../api/client";
import { dateLabel, kindMeta, statusLabel } from "./model";

interface Props {
  api: AudioHarnessApi;
  initial: EngineAsset;
  initialTab?: "diagnosis" | "references";
  busy: boolean;
  message?: string;
  onUpdated: (item: EngineAsset) => void;
  onEdit: (item: EngineAsset) => void;
  onProbe: (item: EngineAsset) => void;
  onLifecycle: (item: EngineAsset, action: "enable" | "disable" | "delete") => void;
  onClose: () => void;
}
export function EngineDetails({ api, initial, initialTab = "diagnosis", busy, message, onUpdated, onEdit, onProbe, onLifecycle, onClose }: Props) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { const frame = requestAnimationFrame(() => heading.current?.focus()); return () => cancelAnimationFrame(frame); }, []);
  const [tab, setTab] = useState<string>(initialTab);
  const [query, setQuery] = useState("");
  const engine = useQuery({ queryKey: ["engine-console-detail", initial.id], queryFn: ({ signal }) => api.getEngine(initial.id, signal), placeholderData: initial, retry: false, refetchOnMount: "always" });
  const item = engine.data ?? initial;
  const missing = engine.error instanceof ApiError && engine.error.status === 404;
  const usage = useQuery({ queryKey: ["engine-console-usage", initial.id], queryFn: ({ signal }) => api.getEngineUsage(initial.id, signal), enabled: !missing, retry: false, refetchOnMount: "always" });
  const models = useQuery({ queryKey: ["engine-models", item.id, item.revision], queryFn: ({ signal }) => api.listEngineModels(item.id, signal), enabled: tab === "models" && item.enabled && item.api === "responses" && !missing && !engine.isError, retry: false, refetchOnMount: "always" });
  const voices = useQuery({ queryKey: ["engine-console-voices", item.id, item.revision], queryFn: ({ signal }) => api.listEnginePresetVoices(item.id, signal), enabled: tab === "voices" && item.enabled && item.api === "qwen_tts_duplex" && !missing && !engine.isError, retry: false, refetchOnMount: "always" });
  useEffect(() => { if (engine.data && !engine.isPlaceholderData) onUpdated(engine.data); }, [engine.data, engine.isPlaceholderData, onUpdated]);
  const [copyState, setCopyState] = useState("");
  const copyId = async (id: string) => {
    try { await navigator.clipboard.writeText(id); setCopyState(`已复制 ${id}`); }
    catch { setCopyState("复制失败，请选中 ID 手动复制。"); }
  };
  const needle = query.trim().toLocaleLowerCase();
  const blocked = busy || engine.isFetching || engine.isError || missing;
  const refresh = () => { void engine.refetch(); void usage.refetch(); };
  const fields = [
    ["处理能力", kindMeta[item.kind].label], ["接入接口", `${item.protocol === "grpc" ? "gRPC" : item.protocol.toUpperCase()} · ${item.api}`],
    ["服务地址", item.target], ["Slug", item.slug], ["配置修订", `r${item.revision}`], ["最近更新", dateLabel(item.updated_at)],
  ];
  const references = <>{usage.isFetching && <p role="status">正在读取引用…</p>}{usage.isError && <div className="alert alert-error" role="alert"><p>引用读取失败：{errorMessage(usage.error)}</p><button className="secondary-button" onClick={() => void usage.refetch()} disabled={usage.isFetching}>重试引用读取</button></div>}{!usage.isFetching && !usage.isError && usage.data && ((usage.data.references ?? []).length ? <ul>{(usage.data.references ?? []).map(reference => <li key={reference}>{reference}</li>)}</ul> : <p>当前没有工作流或智能体引用该引擎。</p>)}</>;
  return <section className="ec-service-detail" aria-label="引擎详情">
    <button className="text-button ec-back" onClick={onClose}>‹ 推理引擎</button>
    <header className="ec-service-heading"><span className={`ec-service-icon ${item.api === "responses" ? "ec-service-llm" : item.api === "qwen_tts_duplex" ? "ec-service-tts" : ""}`} aria-hidden="true" /><div><h1 ref={heading} tabIndex={-1}>{item.name}</h1><p>{kindMeta[item.kind].label}{["responses", "qwen_tts_duplex"].includes(item.api) ? ` · ${item.slug} · ${item.protocol === "grpc" ? "gRPC" : item.protocol.toUpperCase()} / ${item.api}` : ""}</p></div><div className="ec-page-actions"><button className="secondary-button" disabled={blocked || !item.enabled} onClick={() => onProbe(item)}>{busy ? "正在处理…" : "复检"}</button><button className="primary-button" disabled={blocked} onClick={() => onEdit(item)}>编辑配置</button></div></header>
    {engine.isFetching && <p role="status">正在读取最新引擎信息…</p>}
    {engine.isError && <div className="alert alert-error" role="alert"><strong>{missing ? "引擎不存在或已被删除" : "引擎信息读取失败"}</strong><p>{errorMessage(engine.error)}</p>{!missing && <p>以下为上次读取的信息。</p>}<button className="secondary-button" disabled={engine.isFetching || busy} onClick={() => void engine.refetch()}>重新读取</button></div>}
    {message && <p className="ec-operation-message" role="status">{message}</p>}
    {!missing && <Tabs.Root className="ec-service-views" value={tab} onValueChange={next => { setTab(next); setQuery(""); }}>
      <Tabs.List className="ec-detail-tabs" aria-label="引擎详情内容"><Tabs.Trigger value="diagnosis">概览</Tabs.Trigger>{item.api === "responses" && <Tabs.Trigger value="models">模型资源</Tabs.Trigger>}{item.api === "qwen_tts_duplex" && <Tabs.Trigger value="voices">音色资源</Tabs.Trigger>}<Tabs.Trigger value="references">引用关系</Tabs.Trigger></Tabs.List>
      <div className="ec-service-scroll">
        <Tabs.Content value="diagnosis" className="ec-service-overview">
          <div className="ec-service-information"><section><h2>可用性</h2><strong className="ec-availability">{statusLabel(item)}</strong><p className="ec-history-label">最近有效结果：{dateLabel(item.status_observed_at)}{item.status_observed_at ? " · 历史结果" : ""}</p><dl className="ec-diagnostics"><div><dt>健康检查耗时</dt><dd>{item.probe_latency_ms == null ? "—" : `${Math.round(item.probe_latency_ms)} ms`}</dd></div><div><dt>连续失败次数</dt><dd>{item.consecutive_failures}</dd></div><div><dt>最近成功</dt><dd>{dateLabel(item.last_success_at)}</dd></div></dl>{item.status_basis && <p>{item.status_basis}</p>}</section>
          <section><h2>连接信息</h2><dl className="ec-fields">{fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{["服务地址", "Slug", "接入接口"].includes(label) ? <code>{value}</code> : value}</dd></div>)}</dl>{item.description && <p>{item.description}</p>}</section></div>
          <aside className="ec-service-management"><section><h2>引用关系</h2>{usage.data && !usage.isError && <strong>{(usage.data.references ?? []).length} 个引用</strong>}{references}<button className="text-button" onClick={() => setTab("references")}>查看引用关系</button></section><section><h2>服务管理</h2><p>{item.enabled ? "已启用" : "已停用"}</p><p className="muted">停用会停止健康探测并阻止新配置绑定；有引用时不能停用或删除。</p><div className="ec-inline-actions"><button className="secondary-button" disabled={blocked} onClick={() => onLifecycle(item, item.enabled ? "disable" : "enable")}>{item.enabled ? "停用" : "启用"}</button><button className="danger-button" disabled={blocked} onClick={() => onLifecycle(item, "delete")}>删除引擎</button></div></section></aside>
        </Tabs.Content>
        <Tabs.Content value="references"><section className="ec-reference-view"><header>{usage.data && !usage.isError && <span>{(usage.data.references ?? []).length} 个引用</span>}<button className="secondary-button" disabled={usage.isFetching} onClick={() => void usage.refetch()}>刷新引用</button></header><div className="ec-reference-content">{references}<p>工作流与智能体的引用会显示在此处；管理操作会先检查引用。</p></div></section></Tabs.Content>
        <Tabs.Content value="models">
          {!item.enabled ? <p>引擎已停用，启用后可读取模型目录。</p> : <>
            <div className="ec-directory-toolbar"><input aria-label="搜索模型" placeholder="搜索模型名称" value={query} onChange={event => setQuery(event.target.value)} /><span className="ec-directory-count">{models.data ? `${models.data.items.length} 个模型` : ""}</span><button className="secondary-button" disabled={models.isFetching || engine.isError} onClick={() => void models.refetch()}>刷新模型目录</button></div>
            {models.isFetching && <p role="status">正在读取模型目录…</p>}{models.isError && <div className="alert alert-error" role="alert">{errorMessage(models.error, "模型目录读取失败。")}{models.data && <p>以下为上次读取的目录。</p>}</div>}
            {models.data && <>{models.data.items.length === 0 ? <p>引擎未报告可用模型。</p> : <table className="ec-resource-table" aria-label="模型资源"><thead><tr><th scope="col">模型 ID</th><th scope="col">报告状态</th><th scope="col">操作</th></tr></thead><tbody>{models.data.items.filter(model => model.toLocaleLowerCase().includes(needle)).map(model => <tr key={model}><td><code>{model}</code></td><td>服务已报告</td><td><button className="text-button" onClick={() => void copyId(model)}>复制 ID</button></td></tr>)}</tbody></table>}{models.data.items.length > 0 && !models.data.items.some(model => model.toLocaleLowerCase().includes(needle)) && <p>没有符合条件的模型。</p>}</>}
          </>}
        </Tabs.Content>
        <Tabs.Content value="voices">
          {!item.enabled ? <p>引擎已停用，启用后可读取音色目录。</p> : <>
            <div className="ec-directory-toolbar"><input aria-label="搜索音色" placeholder="搜索名称、ID 或语言" value={query} onChange={event => setQuery(event.target.value)} /><span className="ec-directory-count">{voices.data ? `${voices.data.items.length} 个音色 · ${voices.data.serving_mode}` : ""}</span><button className="secondary-button" disabled={voices.isFetching || engine.isError} onClick={() => void voices.refetch()}>刷新音色目录</button></div>
            {voices.isFetching && <p role="status">正在读取音色目录…</p>}{voices.isError && <div className="alert alert-error" role="alert">{errorMessage(voices.error, "音色目录读取失败。")}{voices.data && <p>以下为上次读取的目录。</p>}</div>}
            {voices.data && <>{voices.data.items.length === 0 ? <p>引擎未报告预设音色。</p> : <table className="ec-resource-table ec-voice-table" aria-label="音色资源"><thead><tr><th scope="col">音色</th><th scope="col">语言</th><th scope="col">能力</th><th scope="col">实例可用性</th><th scope="col">操作</th></tr></thead><tbody>{voices.data.items.filter(voice => `${voice.name} ${voice.voice_id} ${voice.language}`.toLocaleLowerCase().includes(needle)).map(voice => <tr key={voice.voice_id}><td><strong>{voice.name}</strong><code>{voice.voice_id}</code></td><td>{voice.language || "未提供语言"}</td><td>{voice.supports_synthesize ? "支持合成" : "不支持合成"} · {voice.supports_duplex ? "支持双工" : "不支持双工"}</td><td>{voice.available_on_instance ? "本实例可用" : "本实例不可用"}</td><td><button className="text-button" onClick={() => void copyId(voice.voice_id)}>复制 ID</button><button className="text-button" disabled title="当前不支持音色试听">试听</button></td></tr>)}</tbody></table>}{voices.data.items.length > 0 && !voices.data.items.some(voice => `${voice.name} ${voice.voice_id} ${voice.language}`.toLocaleLowerCase().includes(needle)) && <p>没有符合条件的音色。</p>}</>}
          </>}
        </Tabs.Content>
        {copyState && <p className="ec-operation-message" role="status">{copyState}</p>}
      </div>
      </Tabs.Root>}
    <button className="text-button ec-detail-refresh" disabled={busy || engine.isFetching || missing} onClick={refresh}>刷新详情</button>
  </section>;
}

interface LifecycleProps { api: AudioHarnessApi; engine: EngineAsset; action: "disable" | "delete"; busy: boolean; onConfirm: () => Promise<void>; onClose: () => void; }
export function EngineLifecycleDialog({ api, engine, action, busy, onConfirm, onClose }: LifecycleProps) {
  const usage = useQuery({ queryKey: ["engine-console-lifecycle-usage", engine.id, action], queryFn: ({ signal }) => api.getEngineUsage(engine.id, signal), retry: false, refetchOnMount: "always" });
  const [failure, setFailure] = useState<string | null>(null);
  const verb = action === "delete" ? "删除" : "停用";
  const referenced = Boolean(usage.data?.references?.length);
  const blocked = busy || usage.isFetching || usage.isError || !usage.data || referenced;
  return <AlertDialog.Root open onOpenChange={open => { if (!open && !busy) onClose(); }}><AlertDialog.Portal><AlertDialog.Overlay className="drawer-backdrop ec-confirm-overlay" /><AlertDialog.Content className="confirm-dialog ec-confirm">
    <AlertDialog.Title>{referenced ? `当前不能${verb}引擎` : `${verb}引擎？`}</AlertDialog.Title><AlertDialog.Description>{referenced ? `“${engine.name}”仍被以下处理链引用，请先解除引用：` : action === "delete" ? `将删除“${engine.name}”，此操作无法撤销。` : `停用“${engine.name}”后将停止健康探测，也不能再绑定到新配置。`}</AlertDialog.Description>
    {usage.isFetching && <p role="status">正在检查引用…</p>}{usage.isError && <div className="alert alert-error" role="alert"><p>引用读取失败：{errorMessage(usage.error)}</p><button className="secondary-button" disabled={usage.isFetching} onClick={() => void usage.refetch()}>重试引用读取</button></div>}
    {referenced && <ul>{usage.data?.references?.map(reference => <li key={reference}>{reference}</li>)}</ul>}{failure && <p className="alert alert-error" role="alert">{failure}</p>}
    <div className="panel-actions"><AlertDialog.Cancel className="secondary-button" disabled={busy}>{referenced ? "知道了" : "取消"}</AlertDialog.Cancel>{!referenced && <button className="danger-button" disabled={blocked} onClick={async () => { setFailure(null); try { await onConfirm(); } catch (reason) { setFailure(errorMessage(reason, `引擎${verb}失败，请重试。`)); } }}>{busy ? "正在处理…" : `确认${verb}`}</button>}</div>
  </AlertDialog.Content></AlertDialog.Portal></AlertDialog.Root>;
}
