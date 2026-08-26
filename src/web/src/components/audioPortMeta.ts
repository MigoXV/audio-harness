export interface AudioPortMeta { label: string; color: string }

const FALLBACK_PORT_META: AudioPortMeta = { label: "未知类型", color: "#69716c" };

export const AUDIO_PORT_META: Readonly<Record<string, AudioPortMeta>> = {
  pcm_chunk: { label: "PCM", color: "#2563eb" },
  vad_audio_chunk: { label: "VAD 专用音频", color: "#0891b2" },
  audio_segment: { label: "音频片段", color: "#c2410c" },
  diarization_segment: { label: "说话人片段", color: "#0e7490" },
  transcription: { label: "转写", color: "#7c3aed" },
  speech_started: { label: "开始事件", color: "#0f766e" },
  speech_stopped: { label: "停止事件", color: "#be123c" },
  embedding: { label: "Embedding", color: "#9a6700" },
  vector_search_result: { label: "检索结果", color: "#b45309" },
  speaker_filter_result: { label: "过滤判定", color: "#a21caf" },
  diarization_local_result: { label: "局部分离", color: "#0369a1" },
  speaker_transcription: { label: "说话人转写", color: "#6d28d9" },
  diarization_cluster_result: { label: "全局聚类", color: "#047857" },
};

export function audioPortMeta(kind: string): AudioPortMeta {
  const fallback = AUDIO_PORT_META[kind] ?? FALLBACK_PORT_META;
  return kind in AUDIO_PORT_META ? fallback : { ...fallback, label: kind };
}
