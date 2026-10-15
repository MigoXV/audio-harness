import { vars } from "../styles/theme.css";

export interface AudioPortMeta { label: string; color: string; shape: "circle" | "square" | "diamond"; line: "solid" | "dashed" }

const audio = { color: vars.color.portAudio, shape: "circle", line: "solid" } as const;
const segment = { color: vars.color.portSegment, shape: "square", line: "solid" } as const;
const transcript = { color: vars.color.portTranscript, shape: "square", line: "solid" } as const;
const event = { color: vars.color.portEvent, shape: "diamond", line: "dashed" } as const;
const vector = { color: vars.color.portVector, shape: "diamond", line: "solid" } as const;
const FALLBACK_PORT_META: AudioPortMeta = { label: "未知类型", ...event };

export const AUDIO_PORT_META: Readonly<Record<string, AudioPortMeta>> = {
  pcm_chunk: { label: "PCM", ...audio },
  vad_audio_chunk: { label: "VAD 专用音频", ...audio },
  audio_segment: { label: "音频片段", ...segment },
  speech_audio_chunk: { label: "流式语音块", ...segment },
  diarization_segment: { label: "说话人片段", ...segment },
  transcription: { label: "转写", ...transcript },
  speech_started: { label: "开始事件", ...event },
  speech_stopped: { label: "停止事件", ...event },
  embedding: { label: "Embedding", ...vector },
  vector_search_result: { label: "检索结果", ...vector },
  speaker_filter_result: { label: "过滤判定", ...event },
  diarization_local_result: { label: "局部分离", ...vector },
  speaker_transcription: { label: "说话人转写", ...transcript },
  diarization_cluster_result: { label: "全局聚类", ...vector },
};

export function audioPortMeta(kind: string): AudioPortMeta {
  const fallback = AUDIO_PORT_META[kind] ?? FALLBACK_PORT_META;
  return kind in AUDIO_PORT_META ? fallback : { ...fallback, label: kind };
}
