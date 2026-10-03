import { MarkerType, type Edge, type Node } from "@xyflow/react";

import type {
  AgentBackend,
  AudioWorkflow,
  EngineAsset,
  VoiceAgentDefinition,
  VoiceAgentValidation,
} from "../../api/types";
import { audioPortMeta } from "../../components/audioPortMeta";
import { vars } from "../../styles/theme.css";

export type VoiceAgentNodeId = "workflow" | "responses" | "backend" | "tts";
export type VoiceAgentNodeState = "boundary" | "configured" | "unconfigured" | "optional" | "invalid";

export interface VoiceAgentNodeData extends Record<string, unknown> {
  label: string;
  subtitle: string;
  summary: string;
  state: VoiceAgentNodeState;
  stateLabel?: string;
  inputs: Array<{ id: string; label: string; color: string }>;
  outputs: Array<{ id: string; label: string; color: string }>;
}

export type VoiceAgentGraphNode = Node<VoiceAgentNodeData>;

export const VOICE_AGENT_GRAPH_COLORS = {
  audio: audioPortMeta("pcm_chunk").color,
  transcript: audioPortMeta("transcription").color,
  delegation: vars.color.portEvent,
  text: vars.color.portTranscript,
  speech: vars.color.portAudio,
} as const;

interface VoiceAgentGraphInput {
  definition: VoiceAgentDefinition;
  workflows: AudioWorkflow[];
  engines: EngineAsset[];
  backends: AgentBackend[];
  diagnostics: VoiceAgentValidation["diagnostics"];
}

function hasDiagnostic(diagnostics: VoiceAgentValidation["diagnostics"], prefix: string): boolean {
  return diagnostics.some((item) => item.field?.startsWith(prefix));
}

function nodeState(
  diagnostics: VoiceAgentValidation["diagnostics"],
  id: VoiceAgentNodeId,
  configured: boolean,
  optional = false,
): VoiceAgentNodeState {
  const prefix = id === "workflow" ? "audio_workflow" : id === "backend" ? "agent_backend" : `${id}_engine`;
  if (hasDiagnostic(diagnostics, prefix)) return "invalid";
  if (configured) return "configured";
  return optional ? "optional" : "unconfigured";
}

function edge(id: string, source: string, sourceHandle: string, target: string, targetHandle: string, color: string): Edge {
  return {
    id, source, sourceHandle, target, targetHandle, type: "smoothstep",
    animated: false,
    style: { stroke: color, strokeWidth: 2.25, strokeDasharray: id === "responses-backend" || id === "backend-responses" ? "6 4" : undefined },
    markerEnd: { type: MarkerType.ArrowClosed, color },
  };
}

export function buildVoiceAgentGraph(input: VoiceAgentGraphInput): { nodes: VoiceAgentGraphNode[]; edges: Edge[] } {
  const { definition, workflows, engines, backends, diagnostics } = input;
  const responseConfig = definition.responses!;
  const ttsConfig = definition.tts!;
  const workflow = workflows.find((item) => item.id === definition.audio_workflow_id);
  const responses = engines.find((item) => item.id === definition.responses_engine_id);
  const tts = engines.find((item) => item.id === definition.tts_engine_id);
  const backend = backends.find((item) => item.id === definition.agent_backend?.backend_id);
  const workflowSummary = workflow?.active_revision == null
    ? "选择已发布工作流"
    : `${workflow.name} · v${workflow.active_revision}`;
  const nodes: VoiceAgentGraphNode[] = [
    { id: "$input", type: "agent", position: { x: 20, y: 180 }, draggable: false, selectable: false, data: { label: "Realtime 输入", subtitle: "SESSION INPUT", summary: "用户语音", state: "boundary", inputs: [], outputs: [{ id: "audio", label: "PCM", color: VOICE_AGENT_GRAPH_COLORS.audio }] } },
    {
      id: "workflow", type: "agent", position: { x: 310, y: 180 },
      data: {
        label: "语音工作流", subtitle: "AUDIO WORKFLOW", summary: workflowSummary,
        state: nodeState(diagnostics, "workflow", workflow?.active_revision != null),
        inputs: [{ id: "audio", label: "PCM", color: VOICE_AGENT_GRAPH_COLORS.audio }],
        outputs: [{ id: "transcript", label: "转写", color: VOICE_AGENT_GRAPH_COLORS.transcript }],
      },
    },
    { id: "responses", type: "agent", position: { x: 650, y: 180 }, data: { label: "Responses", subtitle: "MODEL & POLICY", summary: responses && responseConfig.model ? `${responses.name} · ${responseConfig.model}` : "配置回复模型与策略", state: nodeState(diagnostics, "responses", Boolean(responses?.enabled && responseConfig.model.trim())), inputs: [{ id: "transcript", label: "转写", color: VOICE_AGENT_GRAPH_COLORS.transcript }, { id: "result", label: "委派结果", color: VOICE_AGENT_GRAPH_COLORS.delegation }], outputs: [{ id: "text", label: "回复文本", color: VOICE_AGENT_GRAPH_COLORS.text }, { id: "delegate", label: "任务委派", color: VOICE_AGENT_GRAPH_COLORS.delegation }] } },
    { id: "backend", type: "agent", position: { x: 940, y: 480 }, data: { label: "Agent Backend", subtitle: "OPTIONAL DELEGATION", summary: backend ? `${backend.name} · ${definition.agent_backend?.agent_preset}` : "Responses-only", state: nodeState(diagnostics, "backend", Boolean(backend?.enabled && definition.agent_backend?.cwd.trim()), definition.agent_backend == null), inputs: [{ id: "delegate", label: "任务", color: VOICE_AGENT_GRAPH_COLORS.delegation }], outputs: [{ id: "result", label: "结果", color: VOICE_AGENT_GRAPH_COLORS.delegation }] } },
    { id: "tts", type: "agent", position: { x: 1000, y: 180 }, data: { label: "TTS", subtitle: "SPEECH SYNTHESIS", summary: tts && ttsConfig.default_voice ? `${tts.name} · ${ttsConfig.default_voice}` : "配置合成引擎与音色", state: nodeState(diagnostics, "tts", Boolean(tts?.enabled && ttsConfig.default_voice && ttsConfig.allowed_voices?.length)), inputs: [{ id: "text", label: "文本", color: VOICE_AGENT_GRAPH_COLORS.text }], outputs: [{ id: "speech", label: "音频", color: VOICE_AGENT_GRAPH_COLORS.speech }] } },
    { id: "$output", type: "agent", position: { x: 1290, y: 180 }, draggable: false, selectable: false, data: { label: "Realtime 输出", subtitle: "SESSION OUTPUT", summary: "文本与合成语音", state: "boundary", inputs: [{ id: "speech", label: "音频", color: VOICE_AGENT_GRAPH_COLORS.speech }], outputs: [] } },
  ];

  const agentEdges = [
    edge("responses-tts", "responses", "text", "tts", "text", VOICE_AGENT_GRAPH_COLORS.text),
    edge("tts-output", "tts", "speech", "$output", "speech", VOICE_AGENT_GRAPH_COLORS.speech),
    edge("responses-backend", "responses", "delegate", "backend", "delegate", VOICE_AGENT_GRAPH_COLORS.delegation),
    edge("backend-responses", "backend", "result", "responses", "result", VOICE_AGENT_GRAPH_COLORS.delegation),
  ];
  return {
    nodes,
    edges: [
      edge("input-workflow", "$input", "audio", "workflow", "audio", VOICE_AGENT_GRAPH_COLORS.audio),
      edge("workflow-responses", "workflow", "transcript", "responses", "transcript", VOICE_AGENT_GRAPH_COLORS.transcript),
      ...agentEdges,
    ],
  };
}
