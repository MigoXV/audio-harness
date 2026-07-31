import type {
  EngineApi,
  EngineAsset,
  EngineKind,
  EngineRole,
  EngineStatus,
} from "./api/types";

export const ROLE_LABELS: Record<EngineRole, string> = {
  asr: "语音识别",
  vad: "物理 VAD",
  semantic_vad: "语义 VAD",
};

export const KIND_LABELS: Record<EngineKind, string> = {
  asr: "ASR",
  vad: "VAD",
  asr_vad: "ASR + VAD",
};

export const STATUS_LABELS: Record<EngineStatus, string> = {
  observed_running: "观测运行",
  confirmed_running: "确认运行",
  inactive: "已停用",
};

export function requiredApi(role: EngineRole): EngineApi {
  return role === "asr" ? "ux_speech" : "ux_vad";
}

export function apiForKind(kind: EngineKind): EngineApi {
  return kind === "vad" ? "ux_vad" : "ux_speech";
}

export function bindingCandidates(
  role: EngineRole,
  engines: EngineAsset[],
): EngineAsset[] {
  const api = requiredApi(role);
  return engines.filter((engine) => engine.enabled && engine.api === api);
}

export function formatDate(value: string | null): string {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

export function toDateTimeLocal(value: string | null): string {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 16);
}

export function slugify(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}
