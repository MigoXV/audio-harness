import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import * as Dialog from "@radix-ui/react-dialog";
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  BaseEdge,
  ConnectionLineType,
  Handle,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useNodesInitialized,
  useNodeConnections,
  useReactFlow,
  useUpdateNodeInternals,
  type Connection,
  type ConnectionLineComponentProps,
  type Edge,
  type EdgeChange,
  type EdgeProps,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type {
  AudioHarnessApi,
  AudioNode,
  AudioNodeType,
  AudioWorkflowDocument,
  EngineAsset,
  GraphDiagnostic,
  NodePosition,
  VectorSpace,
} from "../api/types";
import { layoutAudioGraph, needsAutoLayout, orderOutputPorts, orthogonalPath, routeAudioGraph } from "./audioGraphLayout";
import { workflowPortPresentation as portMeta, WORKFLOW_PORT_FAMILIES, type WorkflowPortFamily } from "./workflowPortPresentation";
import { WorkflowPortMarker } from "./WorkflowPortMarker";
import { WorkflowUiIcon } from "./WorkflowUiIcon";
import { WorkflowInspector } from "./WorkflowInspector";
import { collectGraphPortOffsets, type PortOffsets, type RoutePoint } from "./graphLayout";
import { applyAudioGraphTemplate, type AudioGraphTemplateId } from "./audioGraphTemplates";
import { EngineQuickView } from "./EngineQuickView";
import { WorkbenchSidebar } from "./WorkbenchSidebar";
import { WorkflowNodeIcon } from "./WorkflowNodeIcon";
import { workflowNodePresentation, workflowNodeSummary } from "./workflowNodePresentation";
import "./workflowGraph.css";
import { isCompactWorkbench, useInspectorFocus } from "./useInspectorFocus";

interface Props {
  api: AudioHarnessApi;
  onEngineUpdated: (engine: EngineAsset) => void;
  document: AudioWorkflowDocument;
  nodeTypes: AudioNodeType[];
  engines: EngineAsset[];
  vectorSpaces?: VectorSpace[];
  diagnostics: GraphDiagnostic[];
  sidebarOpen: boolean;
  sidebarContent?: ReactNode;
  sidebarFooter?: ReactNode;
  toolbarContent?: ReactNode;
  testHref: string;
  onValidate: () => Promise<void>;
  validating: boolean;
  validationBlockedReason?: string;
  validationNotice?: string | null;
  onSidebarOpenChange: (open: boolean) => void;
  onChange: (document: AudioWorkflowDocument) => void;
}

interface GraphNodeData extends Record<string, unknown> {
  label: string;
  subtitle: string;
  kind: string;
  summary: Array<[string, string]>;
  inputs: Array<{ name: string; kind: string; accepted_kinds?: string[]; label?: string }>;
  outputs: Array<{ name: string; kind: string; label?: string }>;
  role: "input" | "processor" | "output";
  state: "boundary" | "configured" | "unconfigured" | "invalid";
}

type GraphNode = Node<GraphNodeData>;
interface GraphEdgeData extends Record<string, unknown> { route?: RoutePoint[] }
type GraphEdge = Edge<GraphEdgeData>;

const OUTPUTS = [
  { name: "primary_transcript", kind: "transcription", label: "最终转写" },
  { name: "interim_transcript", kind: "transcription", label: "中间转写" },
  { name: "speech_started", kind: "speech_started", label: "语音开始" },
  { name: "speech_stopped", kind: "speech_stopped", label: "最终停顿" },
  { name: "turn_progress", kind: "transcription", label: "轮次进度" },
  { name: "vector_search", kind: "vector_search_result" },
  { name: "speaker_filter", kind: "speaker_filter_result" },
  { name: "diarization_local", kind: "diarization_local_result" },
  { name: "speaker_transcript", kind: "speaker_transcription" },
  { name: "diarization_cluster", kind: "diarization_cluster_result" },
];

const GRAPH_NODE_WIDTH = 232;
const BOUNDARY_GAP = 120;
const ENGINE_DIAGNOSTIC_CODES = new Set([
  "missing_engine",
  "engine_not_found",
  "engine_disabled",
  "engine_api_mismatch",
  "engine_capability_mismatch",
]);

const FIRERED_POSTPROCESSING_DEFAULTS = {
  processor: "firered.stream_vad" as const,
  version: 1 as const,
  parameters: {
    smooth_window_size: 5,
    speech_threshold: 0.5,
    pad_start_frame: 5,
    min_speech_frame: 8,
    max_speech_frame: 2000,
    min_silence_frame: 20,
  },
};

const DIARIZATION_POSTPROCESSING_DEFAULTS = {
  processor: "audio_harness.diarization.vad_coverage" as const,
  version: 1 as const,
};

const FIRERED_PARAMETER_META = [
  ["smooth_window_size", "概率平滑窗口", "帧", 1],
  ["speech_threshold", "语音判定阈值", "0–1", 0.01],
  ["pad_start_frame", "语音起点前补", "帧", 1],
  ["min_speech_frame", "最短连续语音", "帧", 1],
  ["max_speech_frame", "单段最长语音", "帧", 1],
  ["min_silence_frame", "结束所需静音", "帧", 1],
] as const;

const HDBSCAN_PARAMETER_DEFAULTS = {
  min_cluster_size: 2,
  min_samples: 2,
  cluster_selection_epsilon: 0,
  allow_single_cluster: false,
};

const CLUSTERING_POSTPROCESSING_DEFAULTS = {
  processor: "audio_harness.speaker_clustering.complete_link_merge" as const,
  version: 1 as const,
  high_similarity_threshold: 0.70,
  low_similarity_threshold: 0.50,
};

const HDBSCAN_NUMBER_PARAMETER_META = [
  ["min_cluster_size", "最小簇大小", "至少需要形成一个簇的向量数", 2, 1],
  ["min_samples", "最小样本数", "数值越高，噪声判定越保守", 1, 1],
  ["cluster_selection_epsilon", "簇选择距离容差", "增大可能合并距离较近的簇", 0, 0.01],
] as const;

function horizontalAnchor(
  positions: NodePosition[],
  side: "left" | "right",
  fallback: NodePosition,
): NodePosition {
  if (!positions.length) return fallback;
  return positions.reduce((current, position) => {
    if (side === "left") return position.x < current.x ? position : current;
    return position.x > current.x ? position : current;
  });
}

function hasConfiguredEngine(
  node: AudioNode,
  spec: AudioNodeType | undefined,
  engines: readonly EngineAsset[],
): boolean {
  if (spec?.required_api == null) return true;
  const engine = engines.find((item) => item.id === node.engine_id);
  const apiMatches = engine?.api === spec.required_api || (
    spec.required_api === "ux_speaker_diarization" && engine?.api === "dummy_diarize"
  ) || (node.kind === "speaker_clustering" && engine?.api === "dummy_clustering");
  if (!engine?.enabled || !apiMatches) return false;
  if (!spec.required_capability) return true;
  const capabilities = engine.details?.capabilities;
  return Array.isArray(capabilities) && capabilities.includes(spec.required_capability);
}

function PortRow({ port, direction }: { port: { name: string; kind: string; label?: string }; direction: "input" | "output" }) {
  const meta = portMeta(port.kind);
  const connected = useNodeConnections({ handleType: direction === "input" ? "target" : "source", handleId: port.name }).length > 0;
  return <div className={`workflow-port-cell port-${direction}`} title={`${port.name}: ${port.kind}`}>
    <Handle id={port.name} type={direction === "input" ? "target" : "source"}
      position={direction === "input" ? Position.Left : Position.Right}
      className={`workflow-port-handle family-${meta.family}${connected ? " connected" : ""}`}
      style={{ top: "50%", background: "transparent", border: 0 }}
      title={`${port.name}: ${port.kind}${connected ? " · 已连接" : " · 未连接"}`}><WorkflowPortMarker family={meta.family} /></Handle>
    <span>{port.label ?? meta.label}</span>
  </div>;
}

function AudioGraphNode({ data, selected }: NodeProps<GraphNode>) {
  const presentation = workflowNodePresentation(data.kind, data.subtitle);
  const stateLabel = data.state === "invalid" ? "配置错误" : data.state === "unconfigured" ? "待绑定" : null;
  return <article aria-label={`节点 ${data.label}`} title={data.label}
    className={`workflow-graph-node state-${data.state} ${selected ? "selected" : ""}`}
    data-node-role={data.role} data-node-state={data.state}>
    <header className="workflow-node-header">
      <WorkflowNodeIcon icon={presentation.icon} />
      <strong className="workflow-node-title">{presentation.title}</strong>
      {stateLabel && <span className="workflow-node-status">{stateLabel}</span>}
    </header>
    <p className="workflow-node-summary" title={data.summary.map(([label, value]) => `${label}：${value}`).join(" · ")}>{data.summary.map(([, value]) => value).join(" · ")}</p>
    <div className="workflow-node-ports">
      {Array.from({ length: Math.max(data.inputs.length, data.outputs.length) }, (_, i) => <div className="workflow-port-pair" key={i}>
        {data.inputs[i] ? <PortRow port={data.inputs[i]} direction="input" /> : <div />}
        {data.outputs[i] ? <PortRow port={data.outputs[i]} direction="output" /> : <div />}
      </div>)}
    </div>
  </article>;
}

const flowNodeTypes = { audio: AudioGraphNode };

function RoutedAudioEdge({ data, markerEnd, sourceX, sourceY, style, targetX, targetY }: EdgeProps<GraphEdge>) {
  const sourceStubX = sourceX + 36;
  const targetStubX = targetX - 36;
  const middleX = targetX >= sourceStubX + 72
    ? (sourceStubX + targetStubX) / 2
    : sourceStubX;
  const route = data?.route?.length && data.route.length >= 2
    ? data.route
    : [
      { x: sourceX, y: sourceY },
      { x: sourceStubX, y: sourceY },
      { x: middleX, y: sourceY },
      { x: middleX, y: targetY },
      { x: targetX, y: targetY },
    ];
  const path = orthogonalPath(route, 32);
  return <>
    <BaseEdge path={path} className="audio-edge-casing" />
    <BaseEdge path={path} markerEnd={markerEnd} style={style} />
  </>;
}

const flowEdgeTypes = { routed: RoutedAudioEdge };

function RoundedConnectionLine({ fromX, fromY, toX, toY, connectionLineStyle }: ConnectionLineComponentProps<GraphNode>) {
  const sourceStubX = fromX + 36;
  const targetStubX = toX - 36;
  const middleX = toX >= sourceStubX + 72 ? (sourceStubX + targetStubX) / 2 : sourceStubX;
  const path = orthogonalPath([
    { x: fromX, y: fromY },
    { x: sourceStubX, y: fromY },
    { x: middleX, y: fromY },
    { x: middleX, y: toY },
    { x: toX, y: toY },
  ], 32);
  return <path d={path} fill="none" style={connectionLineStyle} />;
}

function nodeConfig(kind: string): AudioNode {
  const id = `${kind.replaceAll("_", "-")}-${crypto.randomUUID().slice(0, 8)}`;
  if (kind === "automatic_gain_control") {
    return {
      id,
      kind,
      engine_id: null,
      config: {
        attack_seconds: 0,
        decay_seconds: 2.5,
        soft_knee_db: 6,
        initial_level_db: -12,
        output_gain_db: 4.5,
      },
    } as AudioNode;
  }
  if (kind === "streaming_denoise") {
    return {
      id,
      kind,
      engine_id: null,
      config: { denoised_weight: null },
      failure_policy: "fail_open",
    } as AudioNode;
  }
  if (kind === "physical_vad") {
    return {
      id,
      kind,
      engine_id: null,
      config: {
        min_segment_seconds: 0.5,
        max_segment_seconds: 60,
        audio_buffer_seconds: 120,
        speech_pre_roll_seconds: 0.3,
        speech_chunk_seconds: 1,
        speech_chunk_holdback_seconds: 1,
        progress_interval_seconds: 0.2,
        postprocessing: null,
      },
    };
  }
  if (kind === "semantic_vad") {
    return {
      id,
      kind,
      engine_id: null,
      config: { max_buffer_seconds: 60 },
      failure_policy: "fail_open",
    };
  }
  if (kind === "audio_embedding") {
    return { id, kind, engine_id: null, config: { algorithm: "eres2netv2", expected_dimension: 192, normalize: true } } as AudioNode;
  }
  if (kind === "streaming_speaker_diarization") {
    return {
      id, kind, engine_id: null,
      config: { input_buffer_latency_ms: 320, min_asr_segment_seconds: 0.5, max_pending_audio_seconds: 120, max_segments: 10000 },
    };
  }
  if (kind === "speaker_diarization") {
    return {
      id,
      kind,
      engine_id: null,
      config: {
        min_batch_seconds: 60,
        max_batch_seconds: 120,
        min_asr_segment_seconds: 0.5,
        postprocessing: structuredClone(DIARIZATION_POSTPROCESSING_DEFAULTS),
      },
    } as AudioNode;
  }
  if (kind === "speaker_clustering") {
    return {
      id,
      kind,
      engine_id: null,
      config: {
        embedding_algorithm: "eres2netv2",
        expected_dimension: 192,
        normalize: true,
        max_embedding_seconds: 60,
        clustering_algorithm: "hdbscan",
        parameters: structuredClone(HDBSCAN_PARAMETER_DEFAULTS),
        postprocessing: structuredClone(CLUSTERING_POSTPROCESSING_DEFAULTS),
        max_vectors: 10000,
      },
      failure_policy: "fail_open",
    } as AudioNode;
  }
  if (kind === "vector_search") {
    return { id, kind, engine_id: null, config: { collection_name: "", vector_name: "", top_k: 10, score_threshold: null, filter: {}, include_embedding: false } } as AudioNode;
  }
  if (kind === "speaker_filter") {
    return { id, kind, engine_id: null, config: { allowed_speaker_ids: ["speaker-001"], min_score: 0.75 } } as AudioNode;
  }
  return { id, kind: "asr", engine_id: null, config: { remove_punctuation: false } };
}

function edgeId(connection: Connection): string {
  const raw = `${connection.source}-${connection.sourceHandle}-${connection.target}-${connection.targetHandle}`;
  return raw.replace(/[^A-Za-z0-9_-]/g, "-");
}

export function AudioGraphEditor({
  ...props
}: Props) {
  return <ReactFlowProvider><AudioGraphEditorInner {...props} /></ReactFlowProvider>;
}

function AudioGraphEditorInner({
  api,
  onEngineUpdated,
  document,
  nodeTypes,
  engines,
  vectorSpaces = [],
  diagnostics,
  sidebarOpen,
  sidebarContent,
  sidebarFooter,
  toolbarContent, testHref, onValidate, validating, validationBlockedReason, validationNotice,
  onSidebarOpenChange,
  onChange,
}: Props) {
  const specs = useMemo(
    () => new Map(nodeTypes.map((item) => [item.kind, item])),
    [nodeTypes],
  );
  const initialNodes = useMemo<GraphNode[]>(() => {
    const positions = document.nodes.flatMap((node) => {
      const position = document.layout?.[node.id];
      return position ? [position] : [];
    });
    const entryPositions = document.edges.flatMap((edge) => {
      if (edge.source.kind !== "input") return [];
      const position = document.layout?.[edge.target.node_id];
      return position ? [position] : [];
    });
    const outputPositions = document.outputs.flatMap((output) => {
      const position = document.layout?.[output.source.node_id];
      return position ? [position] : [];
    });
    const sourceYByOutput = new Map(document.outputs.flatMap((output) => {
      const position = document.layout?.[output.source.node_id];
      return position ? [[output.name, position.y] as const] : [];
    }));
    const outputPorts = orderOutputPorts(OUTPUTS, sourceYByOutput);
    const entryAnchor = entryPositions[0] ?? horizontalAnchor(positions, "left", { x: 384, y: 140 });
    const outputAnchor = horizontalAnchor(outputPositions.length ? outputPositions : positions, "right", { x: 196, y: 140 });
    const values: GraphNode[] = [
      {
        id: "$input",
        type: "audio",
        position: { x: entryAnchor.x - GRAPH_NODE_WIDTH - BOUNDARY_GAP, y: entryAnchor.y },
        deletable: false,
        draggable: false,
        data: {
          label: "PCM 输入",
          subtitle: "WORKFLOW INPUT",
          kind: "input", summary: [["格式", "PCM"]],
          inputs: [],
          outputs: [{ name: "audio", kind: "pcm_chunk" }],
          role: "input",
          state: "boundary",
        },
      },
      ...document.nodes.map((node, index) => {
        const spec = specs.get(node.kind);
        const configured = hasConfiguredEngine(node, spec, engines);
        const invalid = diagnostics.some((item) =>
          item.node_id === node.id && !ENGINE_DIAGNOSTIC_CODES.has(item.code),
        );
        return {
          id: node.id,
          type: "audio",
          position: document.layout?.[node.id] ?? { x: 260 + index * 270, y: 140 },
          data: {
            label: node.id,
            subtitle: spec?.display_name ?? node.kind,
            kind: node.kind, summary: workflowNodeSummary(node, engines),
            inputs: (spec?.inputs ?? []).map((port) => ({
              ...port,
              label: node.kind === "physical_vad" && port.name === "audio"
                ? "主音频"
                : node.kind === "physical_vad" && port.name === "detection_audio"
                  ? "检测音频"
                  : undefined,
            })),
            outputs: (spec?.outputs ?? []).map((port) => ({
              ...port,
              label: node.kind === "physical_vad" && port.name === "speech_stopped"
                ? "物理停顿"
                : node.kind === "semantic_vad" && port.name === "speech_stopped"
                  ? "最终停顿"
                  : node.kind === "semantic_vad" && port.name === "turn_progress"
                    ? "轮次进度"
                    : undefined,
            })),
            role: "processor",
            state: invalid ? "invalid" : configured ? "configured" : "unconfigured",
          },
        } satisfies GraphNode;
      }),
      {
        id: "$outputs",
        type: "audio",
        position: {
          x: outputAnchor.x + GRAPH_NODE_WIDTH + BOUNDARY_GAP,
          y: outputAnchor.y,
        },
        deletable: false,
        draggable: false,
        data: {
          label: "命名出口",
          subtitle: "WORKFLOW OUTPUTS",
          kind: "output", summary: [["已连接", `${document.outputs.length} 个出口`]],
          inputs: outputPorts,
          outputs: [],
          role: "output",
          state: "boundary",
        },
      },
    ];
    return values;
  }, [diagnostics, document.edges, document.layout, document.nodes, document.outputs, engines, specs]);
  const initialEdges = useMemo<GraphEdge[]>(() => {
    const outputKind = (nodeId: string, portName: string): string => {
      const node = document.nodes.find((item) => item.id === nodeId);
      return specs.get(node?.kind ?? "")?.outputs.find((port) => port.name === portName)?.kind ?? "unknown";
    };
    const edgeStyle = (kind: string) => {
      const color = portMeta(kind).color;
      return { type: "routed", className: `audio-edge edge-${kind}`, style: { stroke: color, strokeWidth: portMeta(kind).width } };
    };
    const outputEdges = document.outputs.map((output, index) => ({
      id: `output:${output.name}`,
      source: output.source.node_id,
      sourceHandle: output.source.port,
      target: "$outputs",
      targetHandle: output.name,
      ...edgeStyle(outputKind(output.source.node_id, output.source.port)),
      data: { routeOrder: index },
    }));
    return [
      ...document.edges.map((edge) => ({
        id: `edge:${edge.id}`,
        source: edge.source.kind === "input" ? "$input" : edge.source.node_id,
        sourceHandle: edge.source.kind === "input" ? edge.source.name : edge.source.port,
        target: edge.target.node_id,
        targetHandle: edge.target.port,
        ...edgeStyle(edge.source.kind === "input" ? "pcm_chunk" : outputKind(edge.source.node_id, edge.source.port)),
      })),
      ...outputEdges,
    ];
  }, [document.edges, document.nodes, document.outputs, specs]);
  const [flowNodes, setFlowNodes] = useState(initialNodes);
  const [flowEdges, setFlowEdges] = useState<GraphEdge[]>(initialEdges);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const focusSidebar = useInspectorFocus(selectedId);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [connectingKind, setConnectingKind] = useState<string | null>(null);
  const [pendingTemplate, setPendingTemplate] = useState<AudioGraphTemplateId | null>(null);
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [outputDialogOpen, setOutputDialogOpen] = useState(false);
  const [outputName, setOutputName] = useState("");
  const [outputSource, setOutputSource] = useState("");
  const addOutputButton = useRef<HTMLButtonElement>(null);
  const [nodeSearch, setNodeSearch] = useState("");
  const nodeSearchInput = useRef<HTMLInputElement>(null);
  const [zoom, setZoom] = useState(1);
  useEffect(() => {
    const searchShortcut = (event: KeyboardEvent) => {
      const target = event.target;
      if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey || (target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)))) return;
      event.preventDefault();
      onSidebarOpenChange(true);
      window.requestAnimationFrame(() => nodeSearchInput.current?.focus());
    };
    window.addEventListener("keydown", searchShortcut);
    return () => window.removeEventListener("keydown", searchShortcut);
  }, [onSidebarOpenChange]);
  const nodesInitialized = useNodesInitialized();
  const { fitView, getInternalNode, zoomIn, zoomOut } = useReactFlow<GraphNode, GraphEdge>();
  const updateNodeInternals = useUpdateNodeInternals();
  const checkedInitialLayout = useRef(false);
  const routingVersion = useRef(0);
  const fitAfterGraphChange = useRef(false);

  const readPortOffsets = useCallback((nodes: GraphNode[]): PortOffsets => {
    return collectGraphPortOffsets(
      nodes.map((node) => node.id),
      (nodeId) => getInternalNode(nodeId)?.internals.handleBounds,
    );
  }, [getInternalNode]);

  const applyRoutes = useCallback((nodes: GraphNode[], edges: GraphEdge[]) => {
    const routes = routeAudioGraph(nodes, edges, readPortOffsets(nodes));
    return {
      complete: routes.size === edges.length,
      edges: edges.map((edge) => ({ ...edge, data: { ...(edge.data ?? {}), route: routes.get(edge.id) } })),
    };
  }, [readPortOffsets]);

  const rerouteAfterRender = useCallback((
    nodes: GraphNode[],
    edges: GraphEdge[],
    remeasure = false,
    onComplete?: () => void,
  ) => {
    const version = ++routingVersion.current;
    const route = (attempt: number) => {
      window.requestAnimationFrame(() => {
        if (version !== routingVersion.current) return;
        if (remeasure && typeof window.DOMMatrixReadOnly === "function") {
          updateNodeInternals(nodes.map((node) => node.id));
        }
        window.requestAnimationFrame(() => {
          if (version !== routingVersion.current) return;
          const result = applyRoutes(nodes, edges);
          setFlowEdges(result.edges);
          if (!result.complete && attempt < 4) {
            route(attempt + 1);
            return;
          }
          if (onComplete) window.requestAnimationFrame(onComplete);
        });
      });
    };
    route(0);
  }, [applyRoutes, updateNodeInternals]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setFlowNodes(initialNodes);
      setFlowEdges(initialEdges);
      const shouldFitView = fitAfterGraphChange.current;
      fitAfterGraphChange.current = false;
      rerouteAfterRender(
        initialNodes,
        initialEdges,
        true,
        shouldFitView
          ? () => void fitView({ padding: 0.18, duration: 260, minZoom: 0.15, maxZoom: 1 })
          : undefined,
      );
    }, 0);
    return () => {
      window.clearTimeout(timer);
      routingVersion.current += 1;
    };
  }, [fitView, initialEdges, initialNodes, rerouteAfterRender]);

  const selected = document.nodes.find((node) => node.id === selectedId) ?? null;
  const selectedSpec = selected ? specs.get(selected.kind) : undefined;
  const selectedGraphNode = flowNodes.find((node) => node.id === selectedId) ?? initialNodes.find((node) => node.id === selectedId);
  const inspectorOpen = selectedGraphNode != null;
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      void fitView({ padding: .12, minZoom: .15, maxZoom: 1 });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [fitView, inspectorOpen, sidebarOpen]);
  const selectedEngineConfigured = selected
    ? hasConfiguredEngine(selected, selectedSpec, engines)
    : false;
  const selectedPhysicalConfig = selected?.kind === "physical_vad" ? {
    min_segment_seconds: selected.config?.min_segment_seconds ?? 0.5,
    max_segment_seconds: selected.config?.max_segment_seconds ?? 60,
    audio_buffer_seconds: selected.config?.audio_buffer_seconds ?? 120,
    speech_pre_roll_seconds: selected.config?.speech_pre_roll_seconds ?? 0.3,
    speech_chunk_seconds: selected.config?.speech_chunk_seconds ?? 1,
    speech_chunk_holdback_seconds: selected.config?.speech_chunk_holdback_seconds ?? 1,
    progress_interval_seconds: selected.config?.progress_interval_seconds ?? 0.2,
    postprocessing: selected.config?.postprocessing ?? null,
  } : null;
  const selectedDenoiseConfig = selected?.kind === "streaming_denoise" ? {
    denoised_weight: selected.config?.denoised_weight ?? null,
  } : null;
  const selectedAgcConfig = selected?.kind === "automatic_gain_control" ? {
    attack_seconds: selected.config?.attack_seconds ?? 0,
    decay_seconds: selected.config?.decay_seconds ?? 2.5,
    soft_knee_db: selected.config?.soft_knee_db ?? 6,
    initial_level_db: selected.config?.initial_level_db ?? -12,
    output_gain_db: selected.config?.output_gain_db ?? 4.5,
  } : null;
  const selectedPostprocessing = selectedPhysicalConfig?.postprocessing ? {
    ...selectedPhysicalConfig.postprocessing,
    parameters: {
      ...FIRERED_POSTPROCESSING_DEFAULTS.parameters,
      ...(selectedPhysicalConfig.postprocessing.parameters ?? {}),
    },
  } : null;
  const selectedDiarizationConfig = selected?.kind === "speaker_diarization" ? {
    min_batch_seconds: selected.config?.min_batch_seconds ?? 60,
    max_batch_seconds: selected.config?.max_batch_seconds ?? 120,
    min_asr_segment_seconds: selected.config?.min_asr_segment_seconds ?? 0.5,
    postprocessing: selected.config?.postprocessing === undefined
      ? structuredClone(DIARIZATION_POSTPROCESSING_DEFAULTS)
      : selected.config.postprocessing,
  } : null;
  const selectedStreamingConfig = selected?.kind === "streaming_speaker_diarization" ? {
    input_buffer_latency_ms: 320 as 320 | 640 | 1040,
    min_asr_segment_seconds: 0.5, max_pending_audio_seconds: 120, max_segments: 10000,
    ...selected.config,
  } : null;
  const selectedClusteringConfig = selected?.kind === "speaker_clustering" ? {
    embedding_algorithm: selected.config?.embedding_algorithm ?? "eres2netv2",
    expected_dimension: selected.config?.expected_dimension ?? 192,
    normalize: selected.config?.normalize ?? true,
    max_embedding_seconds: selected.config?.max_embedding_seconds ?? 60,
    clustering_algorithm: selected.config?.clustering_algorithm ?? "hdbscan",
    parameters: {
      ...HDBSCAN_PARAMETER_DEFAULTS,
      ...(selected.config?.parameters ?? {}),
    },
    postprocessing: selected.config?.postprocessing === undefined
      ? structuredClone(CLUSTERING_POSTPROCESSING_DEFAULTS)
      : selected.config.postprocessing,
    max_vectors: selected.config?.max_vectors ?? 10000,
  } : null;

  const arrangeGraph = useCallback(async () => {
    try {
      const offsets = readPortOffsets(flowNodes);
      const { nodes: rawLayoutedNodes, outputOrder } = await layoutAudioGraph(flowNodes, flowEdges, offsets);
      const outputByName = new Map(OUTPUTS.map((port) => [port.name, port]));
      const orderedOutputPorts = [
        ...outputOrder.flatMap((name) => {
          const port = outputByName.get(name);
          return port ? [port] : [];
        }),
        ...OUTPUTS.filter((port) => !outputOrder.includes(port.name)),
      ];
      const layoutedNodes = rawLayoutedNodes.map((node) => node.id === "$outputs"
        ? { ...node, data: { ...node.data, inputs: orderedOutputPorts } }
        : node);
      const layout = Object.fromEntries(
        layoutedNodes
          .filter((node) => node.data.role === "processor")
          .map((node) => [node.id, node.position]),
      );
      setFlowNodes(layoutedNodes);
      const unroutedEdges = flowEdges.map((edge) => ({ ...edge, data: { ...(edge.data ?? {}), route: undefined } }));
      setFlowEdges(unroutedEdges);
      rerouteAfterRender(layoutedNodes, unroutedEdges, true);
      setConnectionError(null);
      onChange({ ...document, layout });
      window.requestAnimationFrame(() => void fitView({ padding: 0.18, duration: 260, minZoom: 0.15, maxZoom: 1 }));
    } catch (reason) {
      setConnectionError(reason instanceof Error ? `自动排列失败：${reason.message}` : "自动排列失败，请稍后重试。");
    }
  }, [document, fitView, flowEdges, flowNodes, onChange, readPortOffsets, rerouteAfterRender]);

  useEffect(() => {
    if (!nodesInitialized || checkedInitialLayout.current) return;
    const expectedIds = new Set(["$input", "$outputs", ...document.nodes.map((node) => node.id)]);
    if (flowNodes.length !== expectedIds.size || flowNodes.some((node) => !expectedIds.has(node.id))) return;
    const positionedNodeIds = new Set(Object.keys(document.layout ?? {}));
    if (needsAutoLayout(flowNodes, document.nodes.map((node) => node.id), positionedNodeIds)) {
      const portCount = flowNodes.reduce((count, node) => count + node.data.inputs.length + node.data.outputs.length, 0);
      if (readPortOffsets(flowNodes).size < portCount) return;
      const frame = window.requestAnimationFrame(() => {
        checkedInitialLayout.current = true;
        void arrangeGraph();
      });
      return () => window.cancelAnimationFrame(frame);
    }
    checkedInitialLayout.current = true;
  }, [arrangeGraph, document.layout, document.nodes, flowNodes, nodesInitialized, readPortOffsets]);

  const portKind = (nodeId: string, handle: string | null, direction: "in" | "out") => {
    if (!handle) return undefined;
    if (nodeId === "$input") return handle === "audio" ? "pcm_chunk" : undefined;
    if (nodeId === "$outputs") return OUTPUTS.find((port) => port.name === handle)?.kind;
    const node = document.nodes.find((item) => item.id === nodeId);
    const spec = node ? specs.get(node.kind) : undefined;
    return (direction === "in" ? spec?.inputs : spec?.outputs)?.find((port) => port.name === handle)?.kind;
  };

  const validConnection = (connection: Connection | Edge): boolean => {
    if (connection.source === "$outputs" || connection.target === "$input") return false;
    const source = portKind(connection.source, connection.sourceHandle ?? null, "out");
    const target = portKind(connection.target, connection.targetHandle ?? null, "in");
    if (!source || !target) return false;
    if (connection.target !== "$outputs") {
      const targetNode = document.nodes.find((item) => item.id === connection.target);
      const targetSpec = targetNode ? specs.get(targetNode.kind) : undefined;
      const targetPort = targetSpec?.inputs.find((port) => port.name === connection.targetHandle);
      if (source !== target && !targetPort?.accepted_kinds?.includes(source)) return false;
      const inputGroup = targetSpec?.input_groups?.find((group) =>
        group.ports.includes(connection.targetHandle ?? ""),
      );
      if (inputGroup) {
        const connected = document.edges.filter((edge) =>
          edge.target.node_id === connection.target && inputGroup.ports.includes(edge.target.port),
        ).length;
        if (connected >= inputGroup.maximum) return false;
      }
    } else if (source !== target) return false;
    return connection.target === "$outputs" || !document.edges.some(
      (edge) => edge.target.node_id === connection.target && edge.target.port === connection.targetHandle,
    );
  };

  const connect = (connection: Connection) => {
    if (!validConnection(connection) || !connection.sourceHandle || !connection.targetHandle) {
      setConnectionError("端口类型不兼容，或目标输入已经被占用。");
      return;
    }
    setConnectionError(null);
    setFlowEdges((current) => addEdge(connection, current));
    if (connection.target === "$outputs") {
      const outputs = document.outputs.filter((item) => item.name !== connection.targetHandle);
      outputs.push({
        name: connection.targetHandle,
        source: { kind: "node", node_id: connection.source, port: connection.sourceHandle },
      });
      onChange({ ...document, outputs });
      return;
    }
    onChange({
      ...document,
      edges: [
        ...document.edges,
        {
          id: edgeId(connection),
          source: connection.source === "$input"
            ? { kind: "input", name: "audio" }
            : { kind: "node", node_id: connection.source, port: connection.sourceHandle },
          target: { node_id: connection.target, port: connection.targetHandle },
        },
      ],
    });
  };

  const unboundOutputs = OUTPUTS.filter((port) => !document.outputs.some((output) => output.name === port.name));
  const outputSources = document.nodes.flatMap((node) => (specs.get(node.kind)?.outputs ?? [])
    .filter((port) => port.kind === OUTPUTS.find((output) => output.name === outputName)?.kind)
    .map((port) => ({ nodeId: node.id, port: port.name, label: `${workflowNodePresentation(node.kind, node.kind).title} · ${node.id} / ${port.name}` })));

  const removeEdges = (removed: Edge[]) => {
    const edgeIds = new Set(removed.filter((edge) => edge.id.startsWith("edge:")).map((edge) => edge.id.slice(5)));
    const outputNames = new Set(removed.filter((edge) => edge.id.startsWith("output:")).map((edge) => edge.id.slice(7)));
    onChange({
      ...document,
      edges: document.edges.filter((edge) => !edgeIds.has(edge.id)),
      outputs: document.outputs.filter((output) => !outputNames.has(output.name)),
    });
  };

  const addNode = (kind: string) => {
    const node = nodeConfig(kind);
    const positions = Object.values(document.layout ?? {});
    const position = {
      x: positions.length ? Math.min(...positions.map((item) => item.x)) : 300,
      y: positions.length ? Math.max(...positions.map((item) => item.y)) + 320 : 100,
    };
    onChange({
      ...document,
      nodes: [...document.nodes, node],
      layout: { ...document.layout, [node.id]: position },
    });
    setSelectedId(node.id);
    if (typeof window.matchMedia === "function" && window.matchMedia("(max-width: 1100px)").matches) onSidebarOpenChange(false);
  };

  const updateNode = (next: AudioNode) => {
    onChange({ ...document, nodes: document.nodes.map((node) => node.id === next.id ? next : node) });
  };

  const deleteSelected = () => {
    if (!selected) return;
    const layout = { ...document.layout };
    delete layout[selected.id];
    onChange({
      ...document,
      nodes: document.nodes.filter((node) => node.id !== selected.id),
      edges: document.edges.filter((edge) =>
        edge.target.node_id !== selected.id && (edge.source.kind === "input" || edge.source.node_id !== selected.id),
      ),
      outputs: document.outputs.filter((output) => output.source.node_id !== selected.id),
      layout,
    });
    setSelectedId(null);
  };

  const chooseTemplate = (template: AudioGraphTemplateId) => {
    setTemplatesOpen(false);
    setPendingTemplate(template);
  };

  const confirmTemplate = () => {
    if (!pendingTemplate) return;
    const next = applyAudioGraphTemplate(document, pendingTemplate, vectorSpaces, engines);
    // 模板的旧预置坐标未包含新版摘要和校验提示的高度。
    // 用户已确认替换整图，因此用实际测量结果重新排列新模板。
    next.layout = {};
    checkedInitialLayout.current = false;
    setSelectedId(null);
    setConnectionError(null);
    setPendingTemplate(null);
    fitAfterGraphChange.current = true;
    onChange(next);
  };

  const onNodesChange = (changes: NodeChange<GraphNode>[]) =>
    setFlowNodes((current) => applyNodeChanges(changes, current));
  const onEdgesChange = (changes: EdgeChange<GraphEdge>[]) =>
    setFlowEdges((current) => applyEdgeChanges(changes, current));

  const focusedEdges = useMemo(() => {
    const hasFocus = Boolean(selectedEdgeId);
    return flowEdges.map((edge) => {
      const related = selectedEdgeId === edge.id || (selectedId != null && (edge.source === selectedId || edge.target === selectedId));
      return { ...edge, className: `${edge.className ?? ""} ${hasFocus ? related ? "edge-focused" : "edge-dimmed" : ""}`.trim() };
    });
  }, [flowEdges, selectedEdgeId, selectedId]);

  return (
    <div className={`audio-builder workflow-builder ${sidebarOpen ? "palette-open" : "palette-closed"} ${inspectorOpen ? "inspector-open" : ""}`}>
      <WorkbenchSidebar
        open={sidebarOpen}
        layout="library"
        expandedTitle="工作流工具"
        collapsedTitle="工作流"
        ariaLabel="工作流侧栏"
        content={sidebarContent}
        footer={sidebarFooter}
        onOpenChange={(open) => { if (open && isCompactWorkbench()) { setSelectedId(null); focusSidebar(); } onSidebarOpenChange(open); }}
      >
        <section className="workflow-node-library" aria-label="节点库">
          <header><h2>节点库</h2><span>{nodeTypes.length + 2}</span>
            <Dialog.Root open={templatesOpen} onOpenChange={setTemplatesOpen}>
              <Dialog.Trigger className="workflow-template-trigger" aria-label="快速模板">模板</Dialog.Trigger>
              <Dialog.Portal><Dialog.Overlay className="drawer-backdrop" />
                <Dialog.Content className="confirm-dialog workflow-template-dialog">
                  <header><Dialog.Title>快速模板</Dialog.Title><Dialog.Close className="icon-button" aria-label="关闭快速模板">×</Dialog.Close></header>
                  <Dialog.Description>选择处理链，确认后替换当前草稿；同类型节点的引擎与配置会保留。</Dialog.Description>
          <div className="audio-catalog-content">
            <p>生成完整处理链与命名出口，只需选择执行引擎。</p>
            <button onClick={() => chooseTemplate("physical_asr")}><strong>物理 VAD + ASR</strong><small>PCM → 物理 VAD → ASR</small></button>
            <button onClick={() => chooseTemplate("physical_semantic_asr")}><strong>物理 + 语义 VAD + ASR</strong><small>PCM → 物理 VAD → 语义 VAD → ASR</small></button>
            <button onClick={() => chooseTemplate("dual_physical_asr")}><strong>物理双路 VAD + ASR</strong><small>正式转写 + AHA 中间结果预览</small></button>
            <button onClick={() => chooseTemplate("dual_physical_semantic_asr")}><strong>双路物理 + 语义 VAD + ASR</strong><small>正式分支语义判停 + AHA 中间结果预览</small></button>
            <button onClick={() => chooseTemplate("voice_agent")}><strong>语音智能体</strong><small>仅允许声纹白名单中的说话人进入转写和智能体</small></button>
            <button onClick={() => chooseTemplate("meeting_streaming")}><strong>流式会议</strong><small>持续识别说话人，保留语义轮次与分角色转写</small></button>
            <button onClick={() => chooseTemplate("meeting_complete")}><strong>会议系统完全体</strong><small>双路语义判停、正式与预览转写、语音特征向量提取与搜索</small></button>
          </div>

                </Dialog.Content>
              </Dialog.Portal>
            </Dialog.Root>
          </header>
          <label className="workflow-library-search"><span className="sr-only">搜索节点</span><WorkflowUiIcon name="search" /><input aria-label="搜索节点" ref={nodeSearchInput} value={nodeSearch} onChange={(event) => setNodeSearch(event.target.value)} placeholder="搜索节点" /><kbd>/</kbd></label>
          <div className="workflow-library-scroll">
          {["输入与前处理", "转写与轮次", "说话人与声纹", "其他节点", "输出"].map((group) => {
            const types = nodeTypes.filter((type) => {
              const meta = workflowNodePresentation(type.kind, type.display_name);
              return meta.group === group && [meta.title, type.display_name, type.kind].some((text) => text.toLowerCase().includes(nodeSearch.toLowerCase().trim()));
            });
            const boundary = group === "输入与前处理" ? "input" : group === "输出" ? "output" : null;
            const boundaryMeta = boundary ? workflowNodePresentation(boundary, "") : null;
            const showBoundary = boundaryMeta && boundaryMeta.title.includes(nodeSearch.trim());
            return types.length || showBoundary ? <section key={group}><h3>{group}</h3>
              {showBoundary && <button onClick={() => void fitView({ nodes: [{ id: boundary === "input" ? "$input" : "$outputs" }], padding: .5, maxZoom: 1 })}><WorkflowNodeIcon icon={boundaryMeta.icon} small /><span>{boundaryMeta.title}</span></button>}
              {types.map((type) => { const meta = workflowNodePresentation(type.kind, type.display_name); return <button key={type.kind} onClick={() => addNode(type.kind)} title={type.display_name}><WorkflowNodeIcon icon={meta.icon} small /><span>{meta.title}</span></button>; })}
            </section> : null;
          })}
          {nodeSearch.trim() && !nodeTypes.some((type) => [type.kind, type.display_name, workflowNodePresentation(type.kind, type.display_name).title].some((text) => text.toLowerCase().includes(nodeSearch.trim().toLowerCase()))) && !["PCM 输入", "命名出口"].some(t => t.includes(nodeSearch.trim())) && <p role="status">没有匹配的节点</p>}
          </div>
        </section>
      </WorkbenchSidebar>

      <section className="workflow-stage">
        <header className="workflow-toolbar">
          {sidebarOpen && <button className="icon-button" aria-label="收起工作流侧栏" onClick={() => onSidebarOpenChange(false)}>‹</button>}
          {toolbarContent}
          <div className="workflow-toolbar-actions">
            <button className="text-button" onClick={() => { onSidebarOpenChange(true); window.requestAnimationFrame(() => nodeSearchInput.current?.focus()); }}><WorkflowUiIcon name="plus" />添加节点</button>
            <button className="text-button" onClick={() => void fitView({ padding: .12, minZoom: .15, maxZoom: 1 })}><WorkflowUiIcon name="sliders" />完整画布</button>
            <button className="text-button" disabled={validating || Boolean(validationBlockedReason)} title={validationBlockedReason} onClick={() => void onValidate()}><WorkflowUiIcon name="check" />{validating ? "校验中…" : "校验"}</button>
            <Link className="primary-button" to={testHref}>测试</Link>
          </div>
        </header>
        <section className="audio-canvas panel workflow-canvas" aria-label="语音工作流画布">
        {validationNotice && <div className="workflow-validation-notice" role="status">{validationNotice}</div>}
        {connectionError && <div className="graph-toast">{connectionError}</div>}
        <ReactFlow
          nodes={flowNodes}
          onMove={(_event, viewport) => setZoom(viewport.zoom)}
          edges={focusedEdges}
          nodeTypes={flowNodeTypes}
          edgeTypes={flowEdgeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onEdgesDelete={removeEdges}
          onConnect={connect}
          onConnectStart={(_event, params) => setConnectingKind(portKind(params.nodeId ?? "", params.handleId, "out") ?? null)}
          onConnectEnd={() => setConnectingKind(null)}
          connectionLineType={ConnectionLineType.SmoothStep}
          connectionLineComponent={RoundedConnectionLine}
          connectionLineStyle={{ stroke: portMeta(connectingKind ?? "").color, strokeWidth: 1.5 }}
          isValidConnection={validConnection}
          onPaneClick={() => { setSelectedId(null); setSelectedEdgeId(null); }}
          onNodeClick={(_event, node) => { setSelectedEdgeId(null); setSelectedId(node.id); if (isCompactWorkbench()) onSidebarOpenChange(false); }}
          onEdgeClick={(_event, edge) => { setSelectedId(null); setSelectedEdgeId(edge.id); }}
          onNodeDragStop={(_event, node) => {
            if (node.id.startsWith("$")) return;
            const nextNodes = flowNodes.map((item) => item.id === node.id ? { ...item, position: node.position } : item);
            rerouteAfterRender(nextNodes, flowEdges);
            onChange({ ...document, layout: { ...document.layout, [node.id]: node.position } });
          }}
          fitView
          fitViewOptions={{ padding: 0.18, minZoom: 0.15, maxZoom: 1 }}
          minZoom={0.15}
          maxZoom={1.6}
          deleteKeyCode={["Backspace", "Delete"]}
        >
          <Panel position="top-left" className="workflow-type-legend" aria-label="连线类型图例">{(Object.keys(WORKFLOW_PORT_FAMILIES) as WorkflowPortFamily[]).map((family) => <span key={family} style={{ color: WORKFLOW_PORT_FAMILIES[family].color }}><i /><WorkflowPortMarker family={family} /><span>{WORKFLOW_PORT_FAMILIES[family].label}</span></span>)}</Panel>
          <Panel position="bottom-left" className="workflow-view-controls">
            <button aria-label="缩小画布" onClick={() => void zoomOut()}>−</button><span>{Math.round(zoom * 100)}%</span>
            <button aria-label="放大画布" onClick={() => void zoomIn()}>＋</button>
            <button aria-label="适配画布" onClick={() => void fitView({ padding: .12, minZoom: .15, maxZoom: 1 })}>⛶</button>
            <button aria-label="自动排列" title="自动排列" onClick={() => void arrangeGraph()}>↔</button>
          </Panel>
        </ReactFlow>
        </section>
      </section>

      {selectedGraphNode && <WorkflowInspector
        key={selectedGraphNode.id}
        title={workflowNodePresentation(selectedGraphNode.data.kind, selectedGraphNode.data.subtitle).title}
        icon={workflowNodePresentation(selectedGraphNode.data.kind, selectedGraphNode.data.subtitle).icon}
        nodeId={selectedGraphNode.id}
        onClose={() => setSelectedId(null)}
        inputs={selectedGraphNode.data.inputs.map((port) => ({ ...port, connections: flowEdges.filter((edge) => edge.target === selectedGraphNode.id && edge.targetHandle === port.name).map((edge) => {
          const peer = flowNodes.find((node) => node.id === edge.source);
          return peer ? workflowNodePresentation(peer.data.kind, peer.data.subtitle).title + (peer.id.startsWith("$") ? "" : ` · ${peer.id}`) : edge.source;
        }) }))}
        outputs={selectedGraphNode.data.outputs.map((port) => ({ ...port, connections: flowEdges.filter((edge) => edge.source === selectedGraphNode.id && edge.sourceHandle === port.name).map((edge) => {
          const peer = flowNodes.find((node) => node.id === edge.target);
          return peer ? workflowNodePresentation(peer.data.kind, peer.data.subtitle).title + (peer.id.startsWith("$") ? "" : ` · ${peer.id}`) : edge.target;
        }) }))}
        basic={selected && selectedSpec ? <>
          {selectedSpec.required_api != null && !selectedEngineConfigured && <p className="workflow-binding-notice">{selected.engine_id ? "所选引擎不可用" : "待绑定执行引擎"}</p>}
          {selectedSpec.required_api != null && <label><span>执行引擎</span><select value={selected.engine_id ?? ""} onChange={(event) => updateNode({ ...selected, engine_id: event.target.value || null } as AudioNode)}><option value="">未绑定</option>{selected.engine_id && !selectedEngineConfigured && <option value={selected.engine_id} disabled>{engines.find((engine) => engine.id === selected.engine_id)?.name ?? selected.engine_id} · 当前不可用</option>}{engines.filter((engine) => engine.enabled && (engine.api === selectedSpec.required_api || (selectedSpec.required_api === "ux_speaker_diarization" && engine.api === "dummy_diarize") || (selected.kind === "speaker_clustering" && engine.api === "dummy_clustering")) && (!selectedSpec.required_capability || (Array.isArray(engine.details?.capabilities) && engine.details.capabilities.includes(selectedSpec.required_capability)))).map((engine) => <option key={engine.id} value={engine.id}>{engine.name} · {engine.status}</option>)}</select><small>需要 {selectedSpec.required_api}{selectedSpec.required_capability ? ` / ${selectedSpec.required_capability}` : ""} 能力</small></label>}
          {selectedSpec.required_api == null && <p className="workflow-binding-notice">此节点使用内置处理，无需绑定执行引擎。</p>}
        </> : undefined}
        advanced={selected && selectedSpec ? <>
          <label><span>节点 ID</span><input value={selected.id} disabled /></label>
          {Object.entries(selected.config ?? {}).filter(([key]) => !["postprocessing", "parameters", "clustering_algorithm", "remove_punctuation", "normalize", "include_embedding", "filter", "collection_name", "vector_name", "progress_interval_seconds", "speech_chunk_seconds", "speech_chunk_holdback_seconds", "allowed_speaker_ids", "min_score", "denoised_weight", "attack_seconds", "decay_seconds", "soft_knee_db", "initial_level_db", "output_gain_db"].includes(key)).map(([key, value]) => <label key={key}><span>{key}</span><input type={typeof value === "number" || value === null ? "number" : "text"} step="0.1" value={value == null ? "" : String(value)} onChange={(event) => updateNode({ ...selected, config: { ...(selected.config ?? {}), [key]: typeof value === "number" || value === null ? (event.target.value ? Number(event.target.value) : null) : event.target.value } } as AudioNode)} /></label>)}
          {selected.kind === "audio_embedding" && <label className="switch-control"><input type="checkbox" checked={selected.config?.normalize ?? true} onChange={(event) => updateNode({ ...selected, config: { algorithm: selected.config?.algorithm ?? "eres2netv2", expected_dimension: selected.config?.expected_dimension ?? 192, normalize: event.target.checked } })} /><span>L2 归一化</span></label>}
          {selected.kind === "vector_search" && <><label><span>Voiceprint Collection</span><select value={selected.config.collection_name} onChange={(event) => { const space = vectorSpaces.find((item) => item.collection_name === event.target.value); if (space) updateNode({ ...selected, config: { ...selected.config, collection_name: space.collection_name, vector_name: space.vectors[0]?.name ?? "" } }); }}><option value="">请选择 voiceprint Collection</option>{vectorSpaces.filter((space) => space.template === "voiceprint").map((space) => <option value={space.collection_name} key={space.collection_name}>{space.collection_name}</option>)}</select></label><label><span>Named Vector</span><select value={selected.config.vector_name} onChange={(event) => updateNode({ ...selected, config: { ...selected.config, vector_name: event.target.value } })}>{vectorSpaces.find((item) => item.collection_name === selected.config.collection_name)?.vectors.map((vector) => <option key={vector.name}>{vector.name}</option>)}</select></label><label><span>Filter JSON</span><textarea value={JSON.stringify(selected.config.filter ?? {}, null, 2)} onChange={(event) => { try { updateNode({ ...selected, config: { ...selected.config, filter: JSON.parse(event.target.value) } }); } catch { /* 保存前由校验提示 */ } }} /></label><label className="switch-control"><input type="checkbox" checked={selected.config.include_embedding} onChange={(event) => updateNode({ ...selected, config: { ...selected.config, include_embedding: event.target.checked } })} /><span>在 AHA 事件中返回查询向量</span></label></>}
          {selected.kind === "speaker_filter" && <><label><span>允许的 Speaker ID</span><textarea value={selected.config.allowed_speaker_ids.join("\n")} onChange={(event) => updateNode({ ...selected, config: { ...selected.config, allowed_speaker_ids: event.target.value.split("\n").map((value) => value.trim()).filter(Boolean) } })} /><small>每行一个，精确且区分大小写。</small></label><label><span>最低匹配分数</span><input type="number" step="0.01" value={selected.config.min_score} onChange={(event) => updateNode({ ...selected, config: { ...selected.config, min_score: Number(event.target.value) } })} /><small>低于该分数、未命中或检索失败时均静默拒绝。</small></label></>}
          {selected.kind === "asr" && <label className="switch-control"><input type="checkbox" checked={selected.config?.remove_punctuation ?? false} onChange={(event) => updateNode({ ...selected, config: { ...selected.config, remove_punctuation: event.target.checked } })} /><span>移除转写标点</span></label>}
          {selected.kind === "streaming_denoise" && selectedDenoiseConfig && <section className="vad-postprocessing-settings">
            <label className="switch-control"><input type="checkbox" checked={selectedDenoiseConfig.denoised_weight != null} onChange={(event) => updateNode({ ...selected, config: { denoised_weight: event.target.checked ? 0.5 : null } })} /><span>混合原音与降噪音频</span><small>关闭时物理 VAD 使用纯降噪音频；下游始终使用原音。</small></label>
            {selectedDenoiseConfig.denoised_weight != null && <label><span>降噪音频权重</span><input type="number" min="0" max="1" step="0.05" value={selectedDenoiseConfig.denoised_weight} onChange={(event) => updateNode({ ...selected, config: { denoised_weight: Number(event.target.value) } })} /><small>输出 = (1-w) × 原音 + w × 降噪音频</small></label>}
          </section>}
          {selected.kind === "automatic_gain_control" && selectedAgcConfig && <section className="vad-postprocessing-settings">
            <div className="inspector-state configured"><strong>无前瞻实时 AGC</strong><span>处理后的 PCM 供全部下游使用</span></div>
            <label><span>压制响应（秒）</span><input type="number" min="0" max="10" step="0.01" value={selectedAgcConfig.attack_seconds} onChange={(event) => updateNode({ ...selected, config: { ...selectedAgcConfig, attack_seconds: Number(event.target.value) } })} /><small>默认 0，声音突然变大时立即压低增益。</small></label>
            <label><span>增益恢复（秒）</span><input type="number" min="0.01" max="60" step="0.1" value={selectedAgcConfig.decay_seconds} onChange={(event) => updateNode({ ...selected, config: { ...selectedAgcConfig, decay_seconds: Number(event.target.value) } })} /><small>默认 2.5 秒。</small></label>
            <label><span>软拐点宽度（dB）</span><input type="number" min="0.01" max="10" step="0.5" value={selectedAgcConfig.soft_knee_db} onChange={(event) => updateNode({ ...selected, config: { ...selectedAgcConfig, soft_knee_db: Number(event.target.value) } })} /></label>
            <label><span>初始检测电平（dBFS）</span><input type="number" min="-80" max="0" step="1" value={selectedAgcConfig.initial_level_db} onChange={(event) => updateNode({ ...selected, config: { ...selectedAgcConfig, initial_level_db: Number(event.target.value) } })} /><small>默认 -12 dBFS，降低流启动时的增益突变。</small></label>
            <label><span>末级补偿（dB）</span><input type="number" min="-24" max="24" step="0.5" value={selectedAgcConfig.output_gain_db} onChange={(event) => updateNode({ ...selected, config: { ...selectedAgcConfig, output_gain_db: Number(event.target.value) } })} /><small>固定曲线：-80/-80 | -55/-42 | -40/-28 | -30/-18 | -20/-13 | -10/-9 | 0/-6</small></label>
          </section>}
          {selected.kind === "physical_vad" && selectedPhysicalConfig && <section className="vad-postprocessing-settings">
            <label><span>流式语音块时长（秒）</span><input type="number" min="0.01" step="0.1" value={selectedPhysicalConfig.speech_chunk_seconds} onChange={(event) => updateNode({ ...selected, config: { ...selectedPhysicalConfig, speech_chunk_seconds: Number(event.target.value) } })} /><small>语音块出口在讲话过程中持续输出；整段出口仍等待分段结束。</small></label>
            <label><span>语音尾部保留（秒）</span><input type="number" min="0" step="0.1" value={selectedPhysicalConfig.speech_chunk_holdback_seconds} onChange={(event) => updateNode({ ...selected, config: { ...selectedPhysicalConfig, speech_chunk_holdback_seconds: Number(event.target.value) } })} /><small>为晚到的停顿边界保留尾部音频。应覆盖 VAD 的结束边界回溯时间。</small></label>
            <label><span>进度发送间隔（秒）</span><input type="number" min="0" step="0.1" value={selectedPhysicalConfig.progress_interval_seconds} onChange={(event) => updateNode({ ...selected, config: { ...selectedPhysicalConfig, progress_interval_seconds: Number(event.target.value) } })} /><small>默认 0.2 秒；设置为 0 时关闭进度事件。</small></label>
            <label className="switch-control"><input type="checkbox" checked={selectedPostprocessing != null} onChange={(event) => updateNode({ ...selected, config: { ...selectedPhysicalConfig, postprocessing: event.target.checked ? structuredClone(FIRERED_POSTPROCESSING_DEFAULTS) : null } })} /><span>自定义 FireRed 后处理</span></label>
            {selectedPostprocessing && <>
              <div className="inspector-state configured"><strong>firered.stream_vad</strong><span>协议版本 v1 · 1 帧 = 10 ms</span></div>
              {FIRERED_PARAMETER_META.map(([key, label, unit, step]) => <label key={key}><span>{label}</span><input type="number" min={key === "speech_threshold" ? 0 : 1} max={key === "speech_threshold" ? 1 : undefined} step={step} value={selectedPostprocessing.parameters[key]} onChange={(event) => updateNode({ ...selected, config: { ...selectedPhysicalConfig, postprocessing: { ...selectedPostprocessing, parameters: { ...selectedPostprocessing.parameters, [key]: Number(event.target.value) } } } })} /><small>{unit}</small></label>)}
            </>}
          </section>}
          {selected.kind === "speaker_diarization" && selectedDiarizationConfig && <section className="diarization-postprocessing-settings">
            <label className="switch-control"><input type="checkbox" checked={selectedDiarizationConfig.postprocessing != null} onChange={(event) => updateNode({ ...selected, config: { ...selectedDiarizationConfig, postprocessing: event.target.checked ? structuredClone(DIARIZATION_POSTPROCESSING_DEFAULTS) : null } })} /><span>启用 VAD 覆盖后处理</span><small>填充有效语音头尾与空洞，合并相邻同说话人片段，并在说话人切换后立即送入 ASR。</small></label>
            {selectedDiarizationConfig.postprocessing && <div className="inspector-state configured"><strong>audio_harness.diarization.vad_coverage</strong><span>协议版本 v1 · 稳定片段流式提交</span></div>}
          </section>}
          {selected.kind === "streaming_speaker_diarization" && selectedStreamingConfig && <section>
            <h3>流式分离参数</h3>
            <p className="muted">连接完整音频与 VAD／语义轮次两个输入；需要 Nemotron-3、16 kHz 单声道。</p>
            <label><span>输入缓冲（毫秒）</span><select value={selectedStreamingConfig.input_buffer_latency_ms ?? 320} onChange={(event) => updateNode({ ...selected, config: { ...selectedStreamingConfig, input_buffer_latency_ms: Number(event.target.value) as 320 | 640 | 1040 } })}>{[320, 640, 1040].map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
            <label><span>最短分角色转写（秒）</span><input type="number" min={0} step={0.1} value={selectedStreamingConfig.min_asr_segment_seconds ?? 0.5} onChange={(event) => updateNode({ ...selected, config: { ...selectedStreamingConfig, min_asr_segment_seconds: Number(event.target.value) } })} /></label>
            <label><span>轮次缓冲与历史保留（秒）</span><input type="number" min={1} value={selectedStreamingConfig.max_pending_audio_seconds ?? 120} onChange={(event) => updateNode({ ...selected, config: { ...selectedStreamingConfig, max_pending_audio_seconds: Number(event.target.value) } })} /></label>
            <label><span>最大片段数</span><input type="number" min={1} max={1000000} step={1} value={selectedStreamingConfig.max_segments ?? 10000} onChange={(event) => updateNode({ ...selected, config: { ...selectedStreamingConfig, max_segments: Number(event.target.value) } })} /></label>
          </section>}
          {selected.kind === "speaker_clustering" && selectedClusteringConfig && <section className="clustering-parameters-settings">
            <label><span>聚类算法</span><select value={selectedClusteringConfig.clustering_algorithm} onChange={(event) => updateNode({ ...selected, config: { ...selectedClusteringConfig, clustering_algorithm: event.target.value as "hdbscan" } })}><option value="hdbscan">HDBSCAN</option></select></label>
            <div className="inspector-state configured"><strong>HDBSCAN 参数模板</strong><span>固定字段 · 严格校验</span></div>
            {HDBSCAN_NUMBER_PARAMETER_META.map(([key, label, help, min, step]) => <label key={key}><span>{label}</span><input type="number" min={min} step={step} value={selectedClusteringConfig.parameters[key]} onChange={(event) => updateNode({ ...selected, config: { ...selectedClusteringConfig, parameters: { ...selectedClusteringConfig.parameters, [key]: Number(event.target.value) } } })} /><small>{help}</small></label>)}
            <label className="switch-control"><input type="checkbox" checked={selectedClusteringConfig.parameters.allow_single_cluster} onChange={(event) => updateNode({ ...selected, config: { ...selectedClusteringConfig, parameters: { ...selectedClusteringConfig.parameters, allow_single_cluster: event.target.checked } } })} /><span>允许所有向量形成单一簇</span><small>开启后更容易把不同说话人融合到同一个簇。</small></label>
            <label className="switch-control"><input type="checkbox" checked={selectedClusteringConfig.postprocessing != null} onChange={(event) => updateNode({ ...selected, config: { ...selectedClusteringConfig, postprocessing: event.target.checked ? structuredClone(CLUSTERING_POSTPROCESSING_DEFAULTS) : null } })} /><span>启用声纹合并后处理</span><small>聚合每个全局说话人的前 20 秒音频，通过双阈值约束合并碎片化身份。</small></label>
            {selectedClusteringConfig.postprocessing && <>
              <div className="inspector-state configured"><strong>完整链接声纹合并</strong><span>每次成功全量快照执行 · 会话内缓存相似度矩阵</span></div>
              <label><span>触发合并高阈值</span><input type="number" min="0" max="1" step="0.01" value={selectedClusteringConfig.postprocessing.high_similarity_threshold} onChange={(event) => updateNode({ ...selected, config: { ...selectedClusteringConfig, postprocessing: { ...selectedClusteringConfig.postprocessing!, high_similarity_threshold: Number(event.target.value) } } })} /><small>默认 0.70；达到该相似度的关系才会尝试合并。</small></label>
              <label><span>组内约束低阈值</span><input type="number" min="0" max="1" step="0.01" value={selectedClusteringConfig.postprocessing.low_similarity_threshold} onChange={(event) => updateNode({ ...selected, config: { ...selectedClusteringConfig, postprocessing: { ...selectedClusteringConfig.postprocessing!, low_similarity_threshold: Number(event.target.value) } } })} /><small>默认 0.50；合并组内任意两人都不得低于该值。</small></label>
            </>}
          </section>}
          {selected.kind === "semantic_vad" && <label><span>失败策略</span><select value={selected.failure_policy} onChange={(event) => updateNode({ ...selected, failure_policy: event.target.value as "fail_open" | "fail_pipeline" })}><option value="fail_open">fail_open</option><option value="fail_pipeline">fail_pipeline</option></select></label>}
          {selected.kind === "streaming_denoise" && <label><span>失败策略</span><select value={selected.failure_policy} onChange={(event) => updateNode({ ...selected, failure_policy: event.target.value as "fail_open" | "fail_pipeline" })}><option value="fail_open">fail_open（旁路原音）</option><option value="fail_pipeline">fail_pipeline（终止会话）</option></select></label>}
          {selected.kind === "speaker_clustering" && <label><span>失败策略</span><select value={selected.failure_policy} onChange={(event) => updateNode({ ...selected, failure_policy: event.target.value as "fail_open" | "fail_pipeline" })}><option value="fail_open">fail_open</option><option value="fail_pipeline">fail_pipeline</option></select></label>}
          {selectedSpec.required_api != null && <EngineQuickView api={api} engineId={selected.engine_id} onUpdated={onEngineUpdated} />}
          <button className="danger-text inspector-delete" onClick={deleteSelected}>删除节点</button>
        </> : undefined}
        diagnostics={diagnostics.filter((item) => item.node_id === selectedGraphNode.id).map((item, index) => <div className="inline-error" key={`${item.message}-${index}`}>{item.message}</div>)}
        portsActions={selectedGraphNode.id === "$outputs" ? <button ref={addOutputButton} className="text-button workflow-add-output" disabled={!unboundOutputs.length} title={unboundOutputs.length ? undefined : "所有命名出口均已连接"} onClick={() => { setOutputName(""); setOutputSource(""); setOutputDialogOpen(true); }}><WorkflowUiIcon name="plus" />添加出口</button> : undefined}
      />}
      <Dialog.Root open={outputDialogOpen} onOpenChange={setOutputDialogOpen}>
        <Dialog.Portal><Dialog.Overlay className="drawer-backdrop" /><Dialog.Content className="confirm-dialog" onEscapeKeyDown={(event) => event.stopPropagation()} onCloseAutoFocus={(event) => {
          event.preventDefault();
          if (addOutputButton.current && !addOutputButton.current.disabled) addOutputButton.current.focus();
          else window.document.querySelector<HTMLElement>(".workflow-inspector > header .icon-button")?.focus();
        }}>
          <Dialog.Title>添加命名出口</Dialog.Title>
          <Dialog.Description>选择未连接的出口及类型兼容的来源端口，连接后保存草稿。</Dialog.Description>
          <label><span>命名出口</span><select value={outputName} onChange={(event) => { setOutputName(event.target.value); setOutputSource(""); }}><option value="">请选择出口</option>{unboundOutputs.map((port) => <option key={port.name} value={port.name}>{port.label ?? portMeta(port.kind).label} · {port.name}</option>)}</select></label>
          <label><span>来源端口</span><select value={outputSource} disabled={!outputName || !outputSources.length} onChange={(event) => setOutputSource(event.target.value)}><option value="">{outputName && !outputSources.length ? "没有类型兼容的来源端口" : "请选择来源端口"}</option>{outputSources.map((source) => <option key={JSON.stringify(source)} value={JSON.stringify(source)}>{source.label}</option>)}</select></label>
          <div className="panel-actions"><Dialog.Close className="secondary-button">取消</Dialog.Close><button className="primary-button" disabled={!outputName || !outputSources.some((source) => JSON.stringify(source) === outputSource)} onClick={() => {
            const source = outputSources.find((item) => JSON.stringify(item) === outputSource);
            if (!source) return;
            connect({ source: source.nodeId, sourceHandle: source.port, target: "$outputs", targetHandle: outputName });
            setOutputDialogOpen(false);
          }}>连接出口</button></div>
        </Dialog.Content></Dialog.Portal>
      </Dialog.Root>
      <AlertDialog.Root open={pendingTemplate !== null} onOpenChange={(open) => { if (!open) setPendingTemplate(null); }}>
        <AlertDialog.Portal>
          <AlertDialog.Overlay className="drawer-backdrop" />
          <AlertDialog.Content className="confirm-dialog">
            <AlertDialog.Title>应用快速模板？</AlertDialog.Title>
            <AlertDialog.Description>当前草稿的节点、连线、出口和布局会替换为标准模板。同类型节点已选择的引擎和配置会保留。</AlertDialog.Description>
            <div className="panel-actions">
              <AlertDialog.Cancel className="secondary-button">取消</AlertDialog.Cancel>
              <AlertDialog.Action className="primary-button" onClick={confirmTemplate}>确认应用</AlertDialog.Action>
            </div>
          </AlertDialog.Content>
        </AlertDialog.Portal>
      </AlertDialog.Root>
    </div>
  );
}
