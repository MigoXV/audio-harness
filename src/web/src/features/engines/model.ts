import type { AudioHarnessApi, EngineApi, EngineAsset, EngineKind, EngineProtocol, EngineWritePayload } from "../../api/types";

export const kindMeta: Record<EngineKind, { label: string; api: EngineApi; protocol: EngineProtocol }> = {
  denoise: { label: "在线降噪", api: "ux_denoise", protocol: "grpc" },
  vad: { label: "VAD", api: "ux_vad", protocol: "grpc" },
  asr: { label: "ASR", api: "ux_speech", protocol: "grpc" },
  diarization: { label: "说话人分离", api: "ux_speaker_diarization", protocol: "grpc" },
  embedding: { label: "音频向量", api: "engine_feature", protocol: "grpc" },
  llm: { label: "Responses", api: "responses", protocol: "https" },
  tts: { label: "TTS", api: "qwen_tts_duplex", protocol: "grpc" },
};
export const kinds = Object.keys(kindMeta) as EngineKind[];
export const statusLabels: Record<EngineAsset["status"], string> = {
  healthy: "健康", unhealthy: "不可用", unknown: "未探测", checking: "探测中", stale: "结果过期", disabled: "已停用",
  observed_running: "观察到运行", confirmed_running: "已确认运行", unavailable: "不可用", inactive: "未运行",
};
export const healthLabels = { all: "全部", healthy: "健康", unhealthy: "异常", unknown: "未探测", disabled: "已停用" } as const;
export type HealthFilter = keyof typeof healthLabels;
export function healthGroup(item: EngineAsset): Exclude<HealthFilter, "all"> {
  if (!item.enabled || item.status === "disabled") return "disabled";
  if (item.status === "healthy") return "healthy";
  if (item.status === "unknown" || item.status === "checking") return "unknown";
  return "unhealthy";
}
export function statusLabel(item: EngineAsset) { return !item.enabled ? "已停用" : statusLabels[item.status]; }
export function dateLabel(value: string | null | undefined) { return value ? new Date(value).toLocaleString("zh-CN") : "—"; }

export async function readCatalog(api: AudioHarnessApi, signal: AbortSignal): Promise<EngineAsset[]> {
  const items = new Map<string, EngineAsset>();
  let offset = 0;
  for (;;) {
    const result = await api.listEngines({ limit: 500, offset }, signal);
    result.items.forEach(item => items.set(item.id, item));
    offset += result.items.length;
    if (offset >= result.total) return [...items.values()];
    if (!result.items.length) throw new Error("引擎目录读取不完整，请重新加载。");
    signal.throwIfAborted();
  }
}

export const configKeys = ["slug", "name", "kind", "api", "protocol", "target", "connect_timeout_seconds", "request_timeout_seconds", "credential_env", "api_key", "enabled", "image", "description", "source", "details"] as const;
export function blank(kind: EngineKind = "asr"): EngineWritePayload {
  return { name: "", slug: "", kind, api: kindMeta[kind].api, protocol: kindMeta[kind].protocol, target: "", connect_timeout_seconds: 10,
    request_timeout_seconds: kind === "llm" ? 180 : null, api_key: null, credential_env: null, enabled: true,
    status: "unknown", image: null, description: null, source: null, details: {} };
}
export function configOf(item: EngineAsset): EngineWritePayload {
  return { ...Object.fromEntries(configKeys.map(key => [key, item[key]])), status: item.status } as EngineWritePayload;
}
export function changedConfig(before: EngineWritePayload, after: EngineWritePayload): Partial<EngineWritePayload> {
  return Object.fromEntries(configKeys.filter(key => JSON.stringify(before[key]) !== JSON.stringify(after[key])).map(key => [key, after[key]]));
}
export type EngineForm = Omit<EngineWritePayload, "connect_timeout_seconds" | "request_timeout_seconds"> & { connect_timeout_seconds: string; request_timeout_seconds: string };
export function formOf(payload: EngineWritePayload): EngineForm {
  return { ...payload, connect_timeout_seconds: String(payload.connect_timeout_seconds ?? 10), request_timeout_seconds: payload.request_timeout_seconds == null ? "" : String(payload.request_timeout_seconds) };
}
export type FieldErrors = Record<string, string>;
export const vadParameters = [
  ["smooth_window_size", "平滑窗口帧数"], ["speech_threshold", "语音阈值"], ["pad_start_frame", "起点前补帧数"],
  ["min_speech_frame", "最短语音帧数"], ["max_speech_frame", "最长语音帧数"], ["min_silence_frame", "最短静音帧数"],
] as const;
export const defaultVad = { processor: "firered.stream_vad", version: 1, parameters: { smooth_window_size: 5, speech_threshold: .5, pad_start_frame: 5, min_speech_frame: 8, max_speech_frame: 2000, min_silence_frame: 20 } };
export function vadOf(details: Record<string, unknown> | undefined): Record<string, number> | null {
  const value = details?.vad_postprocessing;
  if (!value || typeof value !== "object" || !("parameters" in value) || !value.parameters || typeof value.parameters !== "object") return null;
  return value.parameters as Record<string, number>;
}
export function validateForm(form: EngineForm): { payload: EngineWritePayload; errors: FieldErrors } {
  const errors: FieldErrors = {};
  const payload: EngineWritePayload = { ...form, name: form.name.trim(), slug: form.slug.trim(), target: form.target.trim(),
    api_key: form.api_key?.trim() || null, credential_env: form.credential_env?.trim() || null,
    connect_timeout_seconds: Number(form.connect_timeout_seconds), request_timeout_seconds: form.request_timeout_seconds.trim() ? Number(form.request_timeout_seconds) : null };
  for (const [key, label, max] of [["name", "名称", 200], ["slug", "Slug", 100], ["target", "地址", 500]] as const) {
    if (!payload[key] || payload[key].includes("${")) errors[key] = `请填写有效的${label}。`;
    else if (payload[key].length > max) errors[key] = `${label}最多 ${max} 个字符。`;
  }
  if (payload.slug && !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(payload.slug)) errors.slug = "仅使用小写字母、数字及单个连字符。";
  for (const key of ["connect_timeout_seconds", "request_timeout_seconds"] as const) {
    const value = payload[key];
    if (value != null && (!Number.isFinite(value) || value <= 0)) errors[key] = "请输入大于 0 的秒数。";
  }
  if (payload.kind === "llm") {
    try { const url = new URL(payload.target); if (!["http:", "https:"].includes(url.protocol)) throw new Error(); }
    catch { errors.target = "请输入完整的 HTTP(S) API Base URL。"; }
    if (!payload.api_key && !payload.credential_env) errors.api_key = "请填写 API Key 或服务进程环境变量名。";
    if (payload.api_key && payload.api_key.length > 8192) errors.api_key = "API Key 最多 8192 个字符。";
    if (payload.credential_env && (payload.credential_env.length > 200 || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(payload.credential_env))) errors.credential_env = "请输入有效的环境变量名。";
  }
  if (payload.kind === "vad") {
    const vad = vadOf(payload.details);
    if (vad) for (const [key] of vadParameters) {
      const value = vad[key];
      if (!Number.isFinite(value) || (key === "speech_threshold" ? value < 0 || value > 1 : !Number.isInteger(value) || value <= 0)) errors[`vad.${key}`] = key === "speech_threshold" ? "请输入 0 到 1 之间的阈值。" : "请输入大于 0 的整数帧数。";
    }
  }
  return { payload, errors };
}
