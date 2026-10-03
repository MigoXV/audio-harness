import * as AlertDialog from "@radix-ui/react-alert-dialog";
import * as Dialog from "@radix-ui/react-dialog";
import * as Tabs from "@radix-ui/react-tabs";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { AudioHarnessApi, EngineAsset } from "../../api/types";
import { ApiError, errorMessage } from "../../api/client";
import { dateLabel, kindMeta, statusLabel } from "./model";

interface Props {
  api: AudioHarnessApi;
  initial: EngineAsset;
  busy: boolean;
  message?: string;
  onUpdated: (item: EngineAsset) => void;
  onEdit: (item: EngineAsset) => void;
  onProbe: (item: EngineAsset) => void;
  onLifecycle: (item: EngineAsset, action: "enable" | "disable" | "delete") => void;
  onClose: () => void;
  restoreFocus: () => void;
}
export function EngineDetails({ api, initial, busy, message, onUpdated, onEdit, onProbe, onLifecycle, onClose, restoreFocus }: Props) {
  const [tab, setTab] = useState("diagnosis");
  const [query, setQuery] = useState("");
  const engine = useQuery({ queryKey: ["engine-console-detail", initial.id], queryFn: ({ signal }) => api.getEngine(initial.id, signal), placeholderData: initial, retry: false, refetchOnMount: "always" });
  const item = engine.data ?? initial;
  const missing = engine.error instanceof ApiError && engine.error.status === 404;
  const usage = useQuery({ queryKey: ["engine-console-usage", initial.id], queryFn: ({ signal }) => api.getEngineUsage(initial.id, signal), enabled: !missing, retry: false, refetchOnMount: "always" });
  const models = useQuery({ queryKey: ["engine-models", item.id, item.revision], queryFn: ({ signal }) => api.listEngineModels(item.id, signal), enabled: tab === "models" && item.enabled && item.api === "responses" && !missing && !engine.isError, retry: false, refetchOnMount: "always" });
  const voices = useQuery({ queryKey: ["engine-console-voices", item.id, item.revision], queryFn: ({ signal }) => api.listEnginePresetVoices(item.id, signal), enabled: tab === "voices" && item.enabled && item.api === "qwen_tts_duplex" && !missing && !engine.isError, retry: false, refetchOnMount: "always" });
  useEffect(() => { if (engine.data && !engine.isPlaceholderData) onUpdated(engine.data); }, [engine.data, engine.isPlaceholderData, onUpdated]);
  const needle = query.trim().toLocaleLowerCase();
  const blocked = busy || engine.isFetching || engine.isError || missing;
  const refresh = () => { void engine.refetch(); void usage.refetch(); };
  const fields = [
    ["能力", kindMeta[item.kind].label], ["接口", `${item.protocol.toUpperCase()} · ${item.api}`], ["Slug", item.slug], ["地址", item.target],
    ["启用状态", item.enabled ? "已启用" : "已停用"], ["健康状态", statusLabel(item)], ["状态说明", item.status_basis || "尚无状态说明"],
    ["最近探测", dateLabel(item.status_observed_at)], ["最近成功", dateLabel(item.last_success_at)],
    ["探测延迟", item.probe_latency_ms == null ? "—" : `${Math.round(item.probe_latency_ms)} ms`],
    ["连续失败", String(item.consecutive_failures)], ["下次探测", dateLabel(item.next_probe_at)], ["修订", `r${item.revision}`],
    ["最近更新", dateLabel(item.updated_at)],
  ];
  return <Dialog.Root open onOpenChange={open => { if (!open) onClose(); }}><Dialog.Portal><Dialog.Overlay className="drawer-backdrop" /><Dialog.Content className="ec-drawer" onCloseAutoFocus={event => { event.preventDefault(); restoreFocus(); }}>
    <header><div><Dialog.Title>引擎详情</Dialog.Title><Dialog.Description>{item.name} · {kindMeta[item.kind].label}</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="关闭引擎详情">×</Dialog.Close></header>
    <div className="ec-drawer-body">
      {engine.isFetching && <p role="status">正在读取最新引擎信息…</p>}
      {engine.isError && <div className="alert alert-error" role="alert"><strong>{missing ? "引擎不存在或已被删除" : "引擎信息读取失败"}</strong><p>{errorMessage(engine.error)}</p>{!missing && <p>以下为上次读取的信息。</p>}<button className="secondary-button" disabled={engine.isFetching || busy} onClick={() => void engine.refetch()}>重新读取</button></div>}
      {message && <p className="ec-operation-message" role="status">{message}</p>}
      {!missing && <Tabs.Root value={tab} onValueChange={next => { setTab(next); setQuery(""); }}>
        <Tabs.List className="ec-detail-tabs" aria-label="引擎详情内容"><Tabs.Trigger value="diagnosis">诊断与引用</Tabs.Trigger>{item.api === "responses" && <Tabs.Trigger value="models">模型目录</Tabs.Trigger>}{item.api === "qwen_tts_duplex" && <Tabs.Trigger value="voices">音色目录</Tabs.Trigger>}</Tabs.List>
        <Tabs.Content value="diagnosis">
          <dl className="ec-fields">{fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{["地址", "Slug", "接口"].includes(label) ? <code>{value}</code> : value}</dd></div>)}</dl>
          {item.description && <p>{item.description}</p>}
          <section className="ec-detail-section"><h3>工作流与智能体引用</h3>{usage.isFetching && <p role="status">正在读取引用…</p>}{usage.isError && <div className="alert alert-error" role="alert"><p>引用读取失败：{errorMessage(usage.error)}</p><button className="secondary-button" onClick={() => void usage.refetch()} disabled={usage.isFetching}>重试引用读取</button></div>}{!usage.isFetching && !usage.isError && usage.data && ((usage.data.references ?? []).length ? <ul>{(usage.data.references ?? []).map(reference => <li key={reference}>{reference}</li>)}</ul> : <p>当前没有工作流或智能体引用该引擎。</p>)}</section>
          <section className="ec-detail-section"><h3>生命周期</h3><p className="muted">停用会停止健康探测并阻止新配置绑定；有引用时不能停用或删除。</p><div className="ec-inline-actions"><button className="secondary-button" disabled={blocked} onClick={() => onLifecycle(item, item.enabled ? "disable" : "enable")}>{item.enabled ? "停用" : "启用"}</button><button className="danger-button" disabled={blocked} onClick={() => onLifecycle(item, "delete")}>删除引擎</button></div></section>
        </Tabs.Content>
        <Tabs.Content value="models">
          <h3>Responses 模型目录</h3>{!item.enabled ? <p>引擎已停用，启用后可读取模型目录。</p> : <>
            <div className="ec-directory-toolbar"><input aria-label="搜索模型" placeholder="搜索模型名称" value={query} onChange={event => setQuery(event.target.value)} /><button className="secondary-button" disabled={models.isFetching || engine.isError} onClick={() => void models.refetch()}>刷新模型目录</button></div>
            {models.isFetching && <p role="status">正在读取模型目录…</p>}{models.isError && <div className="alert alert-error" role="alert">{errorMessage(models.error, "模型目录读取失败。")}{models.data && <p>以下为上次读取的目录。</p>}</div>}
            {models.data && <><p className="muted">引擎报告 {models.data.items.length} 个模型。</p>{models.data.items.length === 0 ? <p>引擎未报告可用模型。</p> : <ul className="ec-directory-list">{models.data.items.filter(model => model.toLocaleLowerCase().includes(needle)).map(model => <li key={model}><code>{model}</code></li>)}</ul>}{models.data.items.length > 0 && !models.data.items.some(model => model.toLocaleLowerCase().includes(needle)) && <p>没有符合条件的模型。</p>}</>}
          </>}
        </Tabs.Content>
        <Tabs.Content value="voices">
          <h3>TTS 音色目录</h3>{!item.enabled ? <p>引擎已停用，启用后可读取音色目录。</p> : <>
            <div className="ec-directory-toolbar"><input aria-label="搜索音色" placeholder="搜索名称、ID 或语言" value={query} onChange={event => setQuery(event.target.value)} /><button className="secondary-button" disabled={voices.isFetching || engine.isError} onClick={() => void voices.refetch()}>刷新音色目录</button></div>
            {voices.isFetching && <p role="status">正在读取音色目录…</p>}{voices.isError && <div className="alert alert-error" role="alert">{errorMessage(voices.error, "音色目录读取失败。")}{voices.data && <p>以下为上次读取的目录。</p>}</div>}
            {voices.data && <><p className="muted">服务模式：{voices.data.serving_mode} · {voices.data.items.length} 个音色</p>{voices.data.items.length === 0 ? <p>引擎未报告预设音色。</p> : <ul className="ec-directory-list">{voices.data.items.filter(voice => `${voice.name} ${voice.voice_id} ${voice.language}`.toLocaleLowerCase().includes(needle)).map(voice => <li key={voice.voice_id}><strong>{voice.name}</strong><code>{voice.voice_id}</code><span>{voice.language || "未提供语言"} · {voice.supports_synthesize ? "支持合成" : "不支持合成"} · {voice.supports_duplex ? "支持双工" : "不支持双工"}</span><span>{voice.available_on_instance ? "本实例可用" : "本实例不可用"}</span></li>)}</ul>}{voices.data.items.length > 0 && !voices.data.items.some(voice => `${voice.name} ${voice.voice_id} ${voice.language}`.toLocaleLowerCase().includes(needle)) && <p>没有符合条件的音色。</p>}</>}
          </>}
        </Tabs.Content>
      </Tabs.Root>}
    </div>
    <footer><button className="secondary-button" disabled={blocked || !item.enabled} onClick={() => onProbe(item)}>{busy ? "正在处理…" : "复检"}</button><button className="secondary-button" disabled={busy || engine.isFetching || missing} onClick={refresh}>刷新详情</button><button className="primary-button" disabled={blocked} onClick={() => onEdit(item)}>编辑配置</button></footer>
  </Dialog.Content></Dialog.Portal></Dialog.Root>;
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
