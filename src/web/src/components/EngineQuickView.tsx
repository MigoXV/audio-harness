import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { ApiError, errorMessage } from "../api/client";
import type { AudioHarnessApi, EngineAsset } from "../api/types";

interface Props {
  api: AudioHarnessApi;
  engineId: string | null | undefined;
  onUpdated: (engine: EngineAsset) => void;
}

const statusLabels: Record<EngineAsset["status"], string> = {
  healthy: "健康", unhealthy: "不可用", unknown: "未探测", checking: "探测中", stale: "结果过期", disabled: "已停用",
  observed_running: "观察到运行", confirmed_running: "已确认运行", unavailable: "不可用", inactive: "未运行",
};
const kindLabels: Record<EngineAsset["kind"], string> = {
  denoise: "在线降噪", vad: "VAD", asr: "ASR", llm: "Responses", tts: "TTS", embedding: "音频向量", diarization: "说话人分离",
};

export function EngineQuickView({ api, engineId, onUpdated }: Props) {
  const [open, setOpen] = useState(false);
  if (!engineId) return <small>绑定引擎后可查看状态与复检。</small>;

  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <Dialog.Trigger className="text-button engine-quick-view-trigger">查看引擎</Dialog.Trigger>
    <Dialog.Portal>
      {open && <><Dialog.Overlay className="drawer-backdrop" />
        <EngineQuickViewContent key={engineId} api={api} engineId={engineId} onUpdated={onUpdated} />
      </>}
    </Dialog.Portal>
  </Dialog.Root>;
}

function EngineQuickViewContent({ api, engineId, onUpdated }: Props & { engineId: string }) {
  const engine = useQuery({
    queryKey: ["engine-quick-view", engineId],
    queryFn: ({ signal }) => api.getEngine(engineId, signal),
    retry: false,
    refetchOnMount: "always",
  });
  const probe = useMutation({
    mutationFn: () => api.probeEngine(engineId),
    onSuccess: async () => { await engine.refetch(); },
  });
  useEffect(() => { if (engine.data) onUpdated(engine.data); }, [engine.data, onUpdated]);

  const item = engine.data;
  const missing = engine.error instanceof ApiError && engine.error.status === 404;
  const busy = engine.isFetching || probe.isPending;

  return <Dialog.Content className="engine-quick-view" onEscapeKeyDown={(event) => event.stopPropagation()} onKeyDown={(event) => {
    if (event.key === "Escape") event.stopPropagation();
  }}>
    <header><div><Dialog.Title>引擎详情</Dialog.Title><Dialog.Description>查看已绑定引擎的连接与健康状态。</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="关闭引擎详情">×</Dialog.Close></header>
    <div className="engine-quick-view-body">
      {engine.isPending && <p role="status">正在读取引擎…</p>}
      {engine.isError && <div className="alert alert-error" role="alert"><strong>{missing ? "引擎不存在或已被删除" : probe.isSuccess ? "复检已完成，最新信息读取失败" : "引擎信息读取失败"}</strong><p>{errorMessage(engine.error)}</p>{item && !missing && <p>以下为上次读取的信息。</p>}</div>}
      {probe.isError && <div className="alert alert-error" role="alert"><strong>引擎复检失败</strong><p>{errorMessage(probe.error)}</p></div>}
      {probe.isPending && <p role="status">正在复检引擎…</p>}
      {item && !missing && <><h3>{item.name}</h3><dl className="engine-quick-view-fields">
        <div><dt>Slug</dt><dd><code>{item.slug}</code></dd></div>
        <div><dt>能力</dt><dd>{kindLabels[item.kind]}</dd></div>
        <div><dt>接口</dt><dd><code>{item.protocol.toUpperCase()} · {item.api}</code></dd></div>
        <div><dt>地址</dt><dd><code>{item.target}</code></dd></div>
        <div><dt>启用状态</dt><dd>{item.enabled ? "已启用" : "已停用"}</dd></div>
        <div><dt>健康状态</dt><dd>{statusLabels[item.status]}</dd></div>
        <div><dt>状态说明</dt><dd>{item.status_basis || "尚无状态说明"}</dd></div>
        <div><dt>最近探测</dt><dd>{item.status_observed_at ? new Date(item.status_observed_at).toLocaleString("zh-CN") : "尚未探测"}</dd></div>
        <div><dt>探测延迟</dt><dd>{item.probe_latency_ms == null ? "—" : `${Math.round(item.probe_latency_ms)} ms`}</dd></div>
      </dl>{(!item.enabled || item.status === "disabled") && <p className="muted">引擎已停用，无法复检；可在引擎管理页调整。</p>}</>}
    </div>
    <footer><button className="secondary-button" disabled={busy || !item || missing || !item.enabled || item.status === "disabled" || engine.isError} onClick={() => probe.mutate()}>{probe.isPending ? "正在复检…" : "复检"}</button>
      <button className="secondary-button" disabled={busy} onClick={() => { probe.reset(); void engine.refetch(); }}>重新读取</button>
      <Link className="secondary-button" to="/engines">进入引擎管理</Link>
    </footer>
  </Dialog.Content>;
}
