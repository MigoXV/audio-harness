import type {
  EngineApi,
  EngineAsset,
  EngineKind,
  NodeTypeDefinition,
  PipelineDefinition,
  PipelineNode,
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

const NODE_TYPE_LABELS: Record<string, string> = {
  audio_input: "音频输入",
  physical_vad: "物理 VAD",
  semantic_vad: "语义 VAD",
  asr: "语音识别",
  session_output: "会话输出",
};

const KIND_LABELS: Record<string, string> = {
  ...CAPABILITY_LABELS,
};

const STATUS_LABELS: Record<string, string> = {
  observed_running: "待验证",
  confirmed_running: "已确认",
  inactive: "已停用",
};

const KIND_APIS: Record<string, EngineApi> = {
  asr: "ux_speech",
  vad: "ux_vad",
};

const NODE_CAPABILITIES: Record<string, string> = {
  physical_vad: "vad",
};

export const SUPPORTED_KIND_OPTIONS: ReadonlyArray<{
  value: SupportedEngineKind;
  label: string;
}> = [
  { value: "asr", label: "ASR" },
  { value: "vad", label: "VAD" },
];

export function capabilityLabel(capability: string): string {
  return CAPABILITY_LABELS[capability] ?? capability;
}

export function nodeTypeLabel(nodeType: string): string {
  return NODE_TYPE_LABELS[nodeType] ?? nodeType;
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

export function apiForKind(kind: EngineKind): EngineApi | null {
  return KIND_APIS[kind] ?? null;
}

export function nodeCandidates(
  requiredApi: EngineApi | null,
  engines: EngineAsset[],
): EngineAsset[] {
  if (requiredApi === null) {
    return [];
  }
  return engines.filter(
    (engine) => engine.enabled && engine.api === requiredApi,
  );
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

export function nodeDisplayName(
  node: PipelineNode,
  nodeTypes: NodeTypeDefinition[] = [],
): string {
  return (
    node.display_name ??
    nodeTypes.find((item) => item.type === node.type)?.display_name ??
    nodeTypeLabel(node.type)
  );
}

export function nodeCapability(nodeType: string): string {
  return NODE_CAPABILITIES[nodeType] ?? nodeType;
}

export function topologicalNodes(
  definition: PipelineDefinition,
): PipelineNode[] {
  const byId = new Map(definition.nodes.map((node) => [node.id, node]));
  const indegree = new Map(
    definition.nodes.map((node) => [node.id, 0]),
  );
  const outgoing = new Map<string, string[]>();
  for (const edge of definition.edges) {
    if (!byId.has(edge.source_node) || !byId.has(edge.target_node)) {
      continue;
    }
    indegree.set(
      edge.target_node,
      (indegree.get(edge.target_node) ?? 0) + 1,
    );
    outgoing.set(edge.source_node, [
      ...(outgoing.get(edge.source_node) ?? []),
      edge.target_node,
    ]);
  }
  const ready = Array.from(indegree)
    .filter(([, count]) => count === 0)
    .map(([id]) => id)
    .sort();
  const result: PipelineNode[] = [];
  while (ready.length > 0) {
    const id = ready.shift()!;
    const node = byId.get(id);
    if (node) {
      result.push(node);
    }
    for (const target of outgoing.get(id) ?? []) {
      const next = (indegree.get(target) ?? 1) - 1;
      indegree.set(target, next);
      if (next === 0) {
        ready.push(target);
        ready.sort();
      }
    }
  }
  return result.length === definition.nodes.length
    ? result
    : definition.nodes;
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
