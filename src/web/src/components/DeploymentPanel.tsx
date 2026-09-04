interface Props {
  targetType: "audio_workflow" | "voice_agent";
  slug: string;
  activeRevision: number | null;
}

export function DeploymentPanel({ targetType, slug, activeRevision }: Props) {
  if (activeRevision === null) return <section className="panel deployment-empty"><strong>尚不能接入</strong><p>发布后将自动以资源 Slug 对外提供 Realtime model。</p></section>;
  return <section className="panel deployment-card"><div className="panel-heading"><div><p className="eyebrow">REALTIME MODEL</p><h2>已公开接入</h2><p>该资源的 Slug 即 Realtime model，无需额外配置路由。</p></div><span className="status-chip status-published_healthy">已公开</span></div><div className="endpoint-examples"><label><span>Model</span><code>{slug}</code></label><label><span>WebSocket (WSS)</span><code>{targetType === "audio_workflow" ? "/v1/realtime?intent=transcription" : `/v1/realtime?model=${slug}`}</code></label><label><span>WebRTC</span><code>/v1/realtime/calls?model={slug}</code></label></div></section>;
}
