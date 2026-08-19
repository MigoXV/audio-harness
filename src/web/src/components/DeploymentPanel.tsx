import { useMemo, useState } from "react";

import { ApiError, errorMessage } from "../api/client";
import type {
  AudioHarnessApi,
  RealtimeRoute,
} from "../api/types";

interface Props {
  api: AudioHarnessApi;
  targetType: "audio_workflow" | "voice_agent";
  targetId: string;
  slug: string;
  activeRevision: number | null;
  routes: RealtimeRoute[];
  onChanged: () => Promise<void> | void;
}

const modelPattern = /^[a-z0-9][a-z0-9._-]*$/;

export function DeploymentPanel({
  api,
  targetType,
  targetId,
  slug,
  activeRevision,
  routes,
  onChanged,
}: Props) {
  const targetRoutes = useMemo(
    () => routes.filter((route) => route.target_type === targetType && route.target_id === targetId),
    [routes, targetId, targetType],
  );
  const [model, setModel] = useState(slug);
  const [visibility, setVisibility] = useState<"public" | "internal">("public");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<{ target_type?: string; target_id?: string } | null>(null);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  const saveRoute = async (replaceExisting = false) => {
    const normalized = model.trim().toLowerCase();
    if (!modelPattern.test(normalized)) return;
    setBusy(true); setError(null);
    try {
      await api.putRealtimeRoute({
        model: normalized,
        target_type: targetType,
        target_id: targetId,
        visibility,
        replace_existing: replaceExisting,
      });
      setConflict(null);
      await onChanged();
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === "realtime_route_conflict") {
        setConflict((reason.details ?? {}) as { target_type?: string; target_id?: string });
      } else {
        setError(errorMessage(reason, "Realtime 路由保存失败。"));
      }
    } finally { setBusy(false); }
  };

  const removeRoute = async (routeModel: string) => {
    if (pendingDelete !== routeModel) { setPendingDelete(routeModel); return; }
    setBusy(true); setError(null);
    try { await api.deleteRealtimeRoute(routeModel); setPendingDelete(null); await onChanged(); }
    catch (reason) { setError(errorMessage(reason, "Realtime 路由删除失败。")); }
    finally { setBusy(false); }
  };

  if (activeRevision === null) {
    return <section className="panel deployment-empty"><strong>尚不能接入</strong><p>先发布一个活动版本，再配置稳定的对外入口。</p></section>;
  }

  const validModel = modelPattern.test(model.trim().toLowerCase());
  const publicRoute = targetRoutes.find((route) => route.visibility === "public");
  return <div className="deployment-stack">
    {error && <div className="alert alert-error"><span>{error}</span><button className="text-button" onClick={() => setError(null)}>关闭</button></div>}
    <section className="panel deployment-card">
      <div className="panel-heading"><div><p className="eyebrow">REALTIME</p><h2>Realtime 模型路由</h2><p>{targetType === "audio_workflow" ? "WSS 在 session.update 中选择 model；WebRTC 通过信令 URL 选择。" : "语音智能体的 WSS 与 WebRTC 都通过入口 URL 选择 model。"}发布不会自动暴露资源。</p></div><span className={`status-chip ${publicRoute ? "status-published_healthy" : "status-unpublished"}`}>{publicRoute ? "已公开接入" : "未公开接入"}</span></div>
      {targetRoutes.length > 0 && <div className="route-list">{targetRoutes.map((route) => <div className="route-row" key={route.model}><div><code>{route.model}</code><small>{route.visibility === "public" ? "公开" : "内部"} · {route.target_type === "voice_agent" ? "语音智能体" : "语音工作流"}</small></div><div className="row-actions"><button className="text-button" disabled={busy} onClick={() => { setModel(route.model); setVisibility(route.visibility); setConflict(null); }}>编辑</button><button className={pendingDelete === route.model ? "danger-button" : "text-button"} disabled={busy} onClick={() => void removeRoute(route.model)}>{pendingDelete === route.model ? "再次点击确认移除" : "移除"}</button></div></div>)}</div>}
      <div className="route-form"><label><span>Realtime model</span><input value={model} onChange={(event) => { setModel(event.target.value); setConflict(null); }} placeholder={slug} /><small>{validModel ? "小写字母、数字、点、下划线或连字符" : "model 格式不合法"}</small></label><label><span>可见性</span><select value={visibility} onChange={(event) => setVisibility(event.target.value as "public" | "internal")}><option value="public">Public · 可从数据面访问</option><option value="internal">Internal · 仅内部保留</option></select><small>控制该 model 是否允许从数据面访问</small></label><button className="primary-button" disabled={busy || !validModel} onClick={() => void saveRoute(false)}>保存路由</button></div>
      {conflict && <div className="alert alert-warning"><div><strong>这个 model 已被占用</strong><p>当前指向 {conflict.target_type} / {conflict.target_id}。替换后旧目标会立即失去该入口。</p></div><div className="inline-actions"><button className="secondary-button" onClick={() => setConflict(null)}>取消</button><button className="danger-button" disabled={busy} onClick={() => void saveRoute(true)}>确认替换</button></div></div>}
      {publicRoute && <div className="endpoint-examples"><label><span>WebSocket (WSS)</span><code>{targetType === "audio_workflow" ? "/v1/realtime?intent=transcription" : `/v1/realtime?model=${publicRoute.model}`}</code>{targetType === "audio_workflow" && <small>session.update model: {publicRoute.model}</small>}</label><label><span>WebRTC</span><code>/v1/realtime/calls?model={publicRoute.model}</code></label></div>}
    </section>
  </div>;
}
