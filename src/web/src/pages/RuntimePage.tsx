import { useCallback, useEffect, useState } from "react";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi, RuntimeOverview, RuntimeStatus } from "../api/types";

interface Props { api: AudioHarnessApi; refreshVersion: number; }

export function RuntimePage({ api, refreshVersion }: Props) {
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [overview, setOverview] = useState<RuntimeOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { const [status, nextOverview] = await Promise.all([api.getRuntimeStatus(), api.getRuntimeOverview()]); setRuntime(status); setOverview(nextOverview); setError(null); }
    catch (reason) { setError(errorMessage(reason, "运行状态读取失败。")); }
  }, [api]);
  useEffect(() => { void load(); }, [load, refreshVersion]);
  return <main className="workspace-page"><header className="page-heading"><div><p className="eyebrow">RUNTIME</p><h1>运行状态</h1><p>已发布的工作流和智能体会自动以 Slug 公开为 Realtime model。</p></div><button className="secondary-button" onClick={() => void load()}>刷新</button></header>{error && <div className="alert alert-error">{error}</div>}<section className="panel"><h2>Realtime 模型</h2><p>{overview ? `当前已公开 ${overview.realtime.public_routes} 个已发布资源。` : "正在读取…"}</p></section><section className="panel"><h2>容量</h2><p>{runtime ? "运行时已连接。" : "正在读取…"}</p></section></main>;
}
