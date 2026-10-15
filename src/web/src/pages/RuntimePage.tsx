import { useQuery } from "@tanstack/react-query";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi } from "../api/types";

interface Props { api: AudioHarnessApi; refreshVersion: number; }

export function RuntimePage({ api, refreshVersion }: Props) {
  const query = useQuery({
    queryKey: ["runtime-page", refreshVersion],
    queryFn: async () => {
      const [runtime, overview] = await Promise.all([api.getRuntimeStatus(), api.getRuntimeOverview()]);
      return { runtime, overview };
    },
  });
  if (query.isPending) return <main className="workspace-page"><div className="empty-state" role="status">正在读取运行状态…</div></main>;
  const overview = query.data?.overview;
  const runtime = query.data?.runtime;
  return <main className="workspace-page">
    <header className="page-heading"><div><p className="eyebrow">RUNTIME</p><h1>接入与运行</h1><p>已发布的工作流和智能体会自动以 Slug 公开为 Realtime model。</p></div><button className="secondary-button" disabled={query.isFetching} onClick={() => void query.refetch()}>{query.isFetching ? "正在刷新…" : "刷新"}</button></header>
    {query.isError && <div className="alert alert-error" role="alert">{errorMessage(query.error, "运行状态读取失败。请重试。")}</div>}
    {overview && <section className="panel runtime-section" id="access"><div className="panel-heading"><div><h2>Realtime 模型</h2><p>当前已公开 {overview.realtime.public_routes} 个已发布资源。</p></div><span className="status-chip">{overview.status === "ready" ? "服务在线" : overview.status === "unavailable" ? "管理服务不可用" : "待配置"}</span></div>{overview.issues.map((issue) => <div className={`alert alert-${issue.severity}`} key={`${issue.code}-${issue.resource_id ?? issue.scope}`}><strong>{issue.message}</strong></div>)}</section>}
    {runtime && <section className="panel runtime-section"><div className="panel-heading"><div><h2>运行容量</h2><p>当前活动数量、容量与本次运行的累计拒绝次数。</p></div></div><table className="runtime-capacity-table"><thead><tr><th scope="col">通道</th><th scope="col">活动 / 容量</th><th scope="col">拒绝记录</th></tr></thead><tbody>{([['realtime', 'Realtime'], ['webrtc', 'WebRTC'], ['asr', '语音识别'], ['responses', 'Responses'], ['tts', '语音合成']] as const).map(([key, label]) => <tr key={key}><th scope="row">{label}</th><td>{runtime[key].active} / {runtime[key].capacity}</td><td>累计拒绝 {runtime[key].rejected}</td></tr>)}</tbody></table></section>}
  </main>;
}
