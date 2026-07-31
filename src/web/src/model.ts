import type {
  EngineApi,
  EngineAsset,
  EngineBinding,
  EngineKind,
  EngineRole,
  SupportedEngineKind,
} from "./api/types";

const CAPABILITY_LABELS: Record<string, string> = {
  asr: "ASR",
  vad: "VAD",
  semantic_vad: "语义 VAD",
  speaker_embedding: "声纹特征",
  speaker_verification: "声纹验证",
  speaker_diarization: "说话人分离",
  noise_reduction: "降噪",
  punctuation: "标点恢复",
  keyword_spotting: "关键词检测",
  tts: "语音合成",
  voice_conversion: "音色转换",
};

const ROLE_LABELS: Record<string, string> = {
  asr: "语音识别",
  vad: "物理 VAD",
  semantic_vad: "语义 VAD",
};

const KIND_LABELS: Record<string, string> = {
  ...CAPABILITY_LABELS,
};

const STATUS_LABELS: Record<string, string> = {
  observed_running: "待验证",
  confirmed_running: "已确认",
  inactive: "已停用",
};

export const SUPPORTED_KIND_OPTIONS: ReadonlyArray<{
  value: SupportedEngineKind;
  label: string;
}> = [
  { value: "asr", label: "ASR" },
  { value: "vad", label: "VAD" },
];

export interface PipelineStageDefinition {
  role: EngineRole;
  name: string;
  capability: string;
  required: boolean;
  requiredApi: EngineApi;
}

export const PIPELINE_STAGES: readonly PipelineStageDefinition[] = [
  {
    role: "vad",
    name: "物理 VAD",
    capability: "vad",
    required: true,
    requiredApi: "ux_vad",
  },
  {
    role: "semantic_vad",
    name: "语义 VAD",
    capability: "semantic_vad",
    required: false,
    requiredApi: "ux_vad",
  },
  {
    role: "asr",
    name: "语音识别",
    capability: "asr",
    required: true,
    requiredApi: "ux_speech",
  },
];

export function capabilityLabel(capability: string): string {
  return CAPABILITY_LABELS[capability] ?? capability;
}

export function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function protocolLabel(protocol: string): string {
  return protocol === "grpc" ? "gRPC" : protocol;
}

export function requiredApi(role: EngineRole): EngineApi | null {
  if (role === "asr") {
    return "ux_speech";
  }
  if (role === "vad" || role === "semantic_vad") {
    return "ux_vad";
  }
  return null;
}

export function apiForKind(kind: EngineKind): EngineApi | null {
  if (kind === "asr") {
    return "ux_speech";
  }
  if (kind === "vad") {
    return "ux_vad";
  }
  return null;
}

export function bindingCandidates(
  role: EngineRole,
  engines: EngineAsset[],
): EngineAsset[] {
  const api = requiredApi(role);
  if (api === null) {
    return [];
  }
  return engines.filter((engine) => engine.enabled && engine.api === api);
}

export interface EngineState {
  label: string;
  tone: "confirmed" | "pending" | "danger" | "muted";
}

export function engineState(engine: EngineAsset): EngineState {
  if (!engine.enabled || engine.status === "inactive") {
    return { label: "已停用", tone: "muted" };
  }
  if (engine.status === "confirmed_running") {
    return { label: "已确认", tone: "confirmed" };
  }
  if (engine.status === "observed_running") {
    return { label: "待验证", tone: "pending" };
  }
  return { label: statusLabel(engine.status), tone: "muted" };
}

export function stageDefinition(
  binding: EngineBinding,
): PipelineStageDefinition | null {
  return (
    PIPELINE_STAGES.find((stage) => stage.role === binding.role) ?? null
  );
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
