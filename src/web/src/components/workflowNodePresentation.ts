import type { AudioNode, EngineAsset } from "../api/types";

export const WORKFLOW_NODE_TYPES: Record<string, { title: string; code: string; icon: string; group: string }> = {
  input: { title: "PCM 输入", code: "PCM", icon: "mic", group: "输入与前处理" },
  streaming_denoise: { title: "在线降噪", code: "DENOISE", icon: "denoise", group: "输入与前处理" },
  automatic_gain_control: { title: "自动增益控制", code: "AGC", icon: "vad", group: "输入与前处理" },
  physical_vad: { title: "物理 VAD", code: "VAD", icon: "vad", group: "输入与前处理" },
  asr: { title: "语音识别", code: "ASR", icon: "asr", group: "转写与轮次" },
  semantic_vad: { title: "语义 VAD", code: "SEM", icon: "semantic", group: "转写与轮次" },
  speaker_diarization: { title: "说话人分离", code: "DIAR", icon: "people", group: "说话人与声纹" },
  streaming_speaker_diarization: { title: "流式说话人分离", code: "DIAR", icon: "people", group: "说话人与声纹" },
  audio_embedding: { title: "声纹嵌入", code: "EMB", icon: "embed", group: "说话人与声纹" },
  vector_search: { title: "向量检索", code: "SEARCH", icon: "search", group: "说话人与声纹" },
  speaker_filter: { title: "说话人过滤", code: "FILTER", icon: "filter", group: "说话人与声纹" },
  speaker_transcription: { title: "说话人转写", code: "SPK", icon: "speakerText", group: "说话人与声纹" },
  speaker_clustering: { title: "全局聚类", code: "CLUSTER", icon: "cluster", group: "说话人与声纹" },
  output: { title: "命名出口", code: "OUT", icon: "out", group: "输出" },
};

export function workflowNodePresentation(kind: string, title: string) {
  return WORKFLOW_NODE_TYPES[kind] ?? { title, code: kind.toUpperCase(), icon: "vad", group: "其他节点" };
}

export function workflowNodeSummary(node: AudioNode, engines: readonly EngineAsset[]): Array<[string, string]> {
  const config = (node.config ?? {}) as Record<string, unknown>;
  const engine = engines.find((item) => item.id === node.engine_id);
  const service: [string, string] = ["服务", engine ? `${engine.name}${engine.enabled ? "" : " · 已停用"}` : node.engine_id ? "服务不可用" : "待绑定"];
  const value = (key: string, fallback = "未设置") => config[key] == null ? fallback : String(config[key]);
  switch (node.kind) {
    case "vector_search": return [["集合", value("collection_name") || "待选择"], ["向量", value("vector_name") || "待选择"]];
    case "speaker_filter": return [["白名单", `${Array.isArray(config.allowed_speaker_ids) ? config.allowed_speaker_ids.length : 0} 位说话人`], ["最低分数", value("min_score")]];
    case "automatic_gain_control": return [["处理", "实时增益控制"], ["末级补偿", `${value("output_gain_db", "0")} dB`]];
    case "asr": return [service, ["标点", config.remove_punctuation ? "移除标点" : "保留标点"]];
    case "audio_embedding": return [service, ["算法", value("algorithm")], ["归一化", config.normalize === false ? "关闭" : "开启"]];
    case "speaker_clustering": return [service, ["算法", value("clustering_algorithm")]];
    case "physical_vad": return [service, ["最短片段", `${value("min_segment_seconds", "0.5")} 秒`]];
    default: return [service];
  }
}
