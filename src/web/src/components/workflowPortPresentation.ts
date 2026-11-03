import { audioPortMeta } from "./audioPortMeta";
import { vars } from "../styles/theme.css";

export type WorkflowPortFamily = "audioStream" | "audioChunk" | "control" | "embedding" | "result" | "unknown";

// 视觉家族只决定线条和标记；连接兼容性继续由完整 kind / accepted_kinds 校验。
export const WORKFLOW_PORT_FAMILIES = {
  audioStream: { label: "音频流", color: vars.color.portAudio, width: 3.5 },
  audioChunk: { label: "语音片段", color: vars.color.portSegment, width: 2.5 },
  control: { label: "控制 / 事件", color: vars.color.portEvent, width: 1.5 },
  embedding: { label: "特征向量", color: vars.color.portVector, width: 1.5 },
  result: { label: "文本 / 结果", color: vars.color.portTranscript, width: 1.5 },
  unknown: { label: "其他类型", color: vars.color.textMuted, width: 1.5 },
} as const;

export function workflowPortFamily(kind: string): WorkflowPortFamily {
  if (["pcm_chunk", "vad_audio_chunk"].includes(kind)) return "audioStream";
  if (["audio_segment", "speech_audio_chunk", "diarization_segment"].includes(kind)) return "audioChunk";
  if (["speech_started", "speech_stopped"].includes(kind)) return "control";
  if (kind === "embedding") return "embedding";
  if (["transcription", "speaker_transcription", "vector_search_result", "speaker_filter_result", "diarization_local_result", "diarization_cluster_result"].includes(kind)) return "result";
  return "unknown";
}

export function workflowPortPresentation(kind: string) {
  const family = workflowPortFamily(kind);
  const labels: Record<string, string> = { pcm_chunk: "音频", vad_audio_chunk: "检测音频", audio_segment: "语音片段", speech_audio_chunk: "语音块", transcription: "转写流", speech_started: "语音开始" };
  return { ...audioPortMeta(kind), ...WORKFLOW_PORT_FAMILIES[family], label: labels[kind] ?? audioPortMeta(kind).label, family };
}
