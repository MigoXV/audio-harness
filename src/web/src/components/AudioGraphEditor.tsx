import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  Background,
  BaseEdge,
  ConnectionLineType,
  ControlButton,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useNodesInitialized,
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
  AudioNode,
  AudioNodeType,
  AudioWorkflowDocument,
  EngineAsset,
  NodePosition,
  VectorSpace,
} from "../api/types";
import { layoutAudioGraph, needsAutoLayout, orderOutputPorts, orthogonalPath, routeAudioGraph, routingPortKey, type PortOffsets, type RoutePoint } from "./audioGraphLayout";
import { applyAudioGraphTemplate, type AudioGraphTemplateId } from "./audioGraphTemplates";
import { WorkbenchSidebar } from "./WorkbenchSidebar";

interface Props {
  document: AudioWorkflowDocument;
  nodeTypes: AudioNodeType[];
  engines: EngineAsset[];
  vectorSpaces?: VectorSpace[];
  diagnostics: Array<{ message: string; node_id?: string | null; edge_id?: string | null }>;
  sidebarOpen: boolean;
  sidebarContent?: ReactNode;
  sidebarFooter?: ReactNode;
  onSidebarOpenChange: (open: boolean) => void;
  onChange: (document: AudioWorkflowDocument) => void;
}

interface GraphNodeData extends Record<string, unknown> {
  label: string;
  subtitle: string;
  inputs: Array<{ name: string; kind: string; accepted_kinds?: string[]; label?: string }>;
  outputs: Array<{ name: string; kind: string; label?: string }>;
  role: "input" | "processor" | "output";
  state: "boundary" | "configured" | "unconfigured" | "invalid";
}

type GraphNode = Node<GraphNodeData>;
interface GraphEdgeData extends Record<string, unknown> { route?: RoutePoint[] }
type GraphEdge = Edge<GraphEdgeData>;

const OUTPUTS = [
  { name: "primary_transcript", kind: "transcription" },
  { name: "interim_transcript", kind: "transcription" },
  { name: "speech_started", kind: "speech_started", label: "开始事件" },
  { name: "speech_stopped", kind: "speech_stopped", label: "最终停顿" },
  { name: "turn_progress", kind: "transcription", label: "轮次未结束" },
  { name: "vector_search", kind: "vector_search_result" },
  { name: "diarization_local", kind: "diarization_local_result" },
  { name: "speaker_transcript", kind: "speaker_transcription" },
  { name: "diarization_cluster", kind: "diarization_cluster_result" },
];

const PORT_META: Record<string, { label: string; color: string }> = {
  pcm_chunk: { label: "PCM", color: "#2563eb" },
  audio_segment: { label: "音频片段", color: "#c2410c" },
  diarization_segment: { label: "说话人片段", color: "#0e7490" },
  transcription: { label: "转写", color: "#7c3aed" },
  speech_started: { label: "开始事件", color: "#0f766e" },
  speech_stopped: { label: "停止事件", color: "#be123c" },
  embedding: { label: "Embedding", color: "#9a6700" },
  vector_search_result: { label: "检索结果", color: "#b45309" },
  diarization_local_result: { label: "局部分离", color: "#0369a1" },
  speaker_transcription: { label: "说话人转写", color: "#6d28d9" },
  diarization_cluster_result: { label: "全局聚类", color: "#047857" },
};

const ROLE_COLORS = {
  input: "#2563eb",
  processor: "#28684f",
  output: "#7c3aed",
} as const;

const GRAPH_NODE_WIDTH = 244;
const BOUNDARY_GAP = 120;

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

function portMeta(kind: string): { label: string; color: string } {
  return PORT_META[kind] ?? { label: kind, color: "#69716c" };
}

function PortRow({ port, direction }: { port: { name: string; kind: string; label?: string }; direction: "input" | "output" }) {
  const meta = portMeta(port.kind);
  return <li className={`audio-port-row port-${direction}`}>
    <Handle
      id={port.name}
      type={direction === "input" ? "target" : "source"}
      position={direction === "input" ? Position.Left : Position.Right}
      style={{ top: "50%", background: meta.color }}
      title={`${port.name}: ${port.kind}`}
    />
    <span className="audio-port-name">{port.name}</span>
    <code style={{ color: meta.color }}>{port.label ?? meta.label}</code>
  </li>;
}

function AudioGraphNode({ data, selected }: NodeProps<GraphNode>) {
  const stateLabel = data.state === "invalid" ? "配置错误" : data.state === "unconfigured" ? "待配置" : null;
  return (
    <article
      aria-label={`节点 ${data.label}`}
      className={`audio-graph-node role-${data.role} state-${data.state} ${selected ? "selected" : ""}`}
      data-node-role={data.role}
      data-node-state={data.state}
    >
      <header className="audio-node-header">
        <div><span className="audio-node-kind">{data.subtitle}</span>{stateLabel && <span className="audio-node-state">{stateLabel}</span>}</div>
        <strong>{data.label}</strong>
      </header>
      <div className="audio-node-port-groups">
        {data.inputs.length > 0 && <section className="audio-port-group input-ports"><span>输入</span><ul>{data.inputs.map((port) => <PortRow key={port.name} port={port} direction="input" />)}</ul></section>}
        {data.outputs.length > 0 && <section className="audio-port-group output-ports"><span>输出</span><ul>{data.outputs.map((port) => <PortRow key={port.name} port={port} direction="output" />)}</ul></section>}
      </div>
    </article>
  );
}

const flowNodeTypes = { audio: AudioGraphNode };

function RoutedAudioEdge({ data, markerEnd, style }: EdgeProps<GraphEdge>) {
  const route = data?.route ?? [];
  const path = route.length >= 2 ? orthogonalPath(route) : "";
  if (!path) return null;
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
  ]);
  return <path d={path} fill="none" style={connectionLineStyle} />;
}

function nodeConfig(kind: string): AudioNode {
  const id = `${kind.replaceAll("_", "-")}-${crypto.randomUUID().slice(0, 8)}`;
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
        max_vectors: 10000,
      },
      failure_policy: "fail_open",
    } as AudioNode;
  }
  if (kind === "vector_search") {
    return { id, kind, engine_id: null, config: { collection_name: "", vector_name: "", top_k: 10, score_threshold: null, filter: {}, include_embedding: false } } as AudioNode;
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
  document,
  nodeTypes,
  engines,
  vectorSpaces = [],
  diagnostics,
  sidebarOpen,
  sidebarContent,
  sidebarFooter,
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
          inputs: [],
          outputs: [{ name: "audio", kind: "pcm_chunk" }],
          role: "input",
          state: "boundary",
        },
      },
      ...document.nodes.map((node, index) => {
        const spec = specs.get(node.kind);
        const invalid = diagnostics.some((item) => item.node_id === node.id);
        return {
          id: node.id,
          type: "audio",
          position: document.layout?.[node.id] ?? { x: 260 + index * 270, y: 140 },
          data: {
            label: node.id,
            subtitle: spec?.display_name ?? node.kind,
            inputs: spec?.inputs ?? [],
            outputs: (spec?.outputs ?? []).map((port) => ({
              ...port,
              label: node.kind === "physical_vad" && port.name === "speech_stopped"
                ? "物理停顿"
                : node.kind === "semantic_vad" && port.name === "speech_stopped"
                  ? "最终停顿"
                  : node.kind === "semantic_vad" && port.name === "turn_progress"
                    ? "轮次未结束"
                    : undefined,
            })),
            role: "processor",
            state: invalid ? "invalid" : node.engine_id || spec?.required_api == null ? "configured" : "unconfigured",
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
          inputs: outputPorts,
          outputs: [],
          role: "output",
          state: "boundary",
        },
      },
    ];
    return values;
  }, [diagnostics, document.edges, document.layout, document.nodes, document.outputs, specs]);
  const initialEdges = useMemo<GraphEdge[]>(() => {
    const outputKind = (nodeId: string, portName: string): string => {
      const node = document.nodes.find((item) => item.id === nodeId);
      return specs.get(node?.kind ?? "")?.outputs.find((port) => port.name === portName)?.kind ?? "unknown";
    };
    const edgeStyle = (kind: string) => {
      const color = portMeta(kind).color;
      return { type: "routed", className: `audio-edge edge-${kind}`, style: { stroke: color, strokeWidth: 2.25 }, markerEnd: { type: MarkerType.ArrowClosed, color } };
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
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [connectingKind, setConnectingKind] = useState<string | null>(null);
  const [pendingTemplate, setPendingTemplate] = useState<AudioGraphTemplateId | null>(null);
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [nodeCatalogOpen, setNodeCatalogOpen] = useState(false);
  const nodesInitialized = useNodesInitialized();
  const { fitView, getInternalNode } = useReactFlow<GraphNode, GraphEdge>();
  const updateNodeInternals = useUpdateNodeInternals();
  const checkedInitialLayout = useRef(false);

  const readPortOffsets = useCallback((nodes: GraphNode[]): PortOffsets => {
    const offsets = new Map<string, { x: number; y: number; width: number; height: number }>();
    nodes.forEach((node) => {
      const bounds = getInternalNode(node.id)?.internals.handleBounds;
      bounds?.target?.forEach((handle) => offsets.set(
        routingPortKey(node.id, "input", handle.id),
        { x: handle.x + handle.width / 2, y: handle.y + handle.height / 2, width: handle.width, height: handle.height },
      ));
      bounds?.source?.forEach((handle) => offsets.set(
        routingPortKey(node.id, "output", handle.id),
        { x: handle.x + handle.width / 2, y: handle.y + handle.height / 2, width: handle.width, height: handle.height },
      ));
    });
    return offsets;
  }, [getInternalNode]);

  const applyRoutes = useCallback((nodes: GraphNode[], edges: GraphEdge[]): GraphEdge[] => {
    const routes = routeAudioGraph(nodes, edges, readPortOffsets(nodes));
    return edges.map((edge) => ({ ...edge, data: { ...(edge.data ?? {}), route: routes.get(edge.id) } }));
  }, [readPortOffsets]);

  const rerouteAfterRender = useCallback((nodes: GraphNode[], edges: GraphEdge[], remeasure = false) => {
    window.requestAnimationFrame(() => {
      if (remeasure && typeof window.DOMMatrixReadOnly === "function") updateNodeInternals(nodes.map((node) => node.id));
      window.requestAnimationFrame(() => setFlowEdges(applyRoutes(nodes, edges)));
    });
  }, [applyRoutes, updateNodeInternals]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setFlowNodes(initialNodes);
      setFlowEdges(initialEdges);
      rerouteAfterRender(initialNodes, initialEdges, true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [initialEdges, initialNodes, rerouteAfterRender]);

  const selected = document.nodes.find((node) => node.id === selectedId) ?? null;
  const selectedSpec = selected ? specs.get(selected.kind) : undefined;
  const selectedPhysicalConfig = selected?.kind === "physical_vad" ? {
    min_segment_seconds: selected.config?.min_segment_seconds ?? 0.5,
    max_segment_seconds: selected.config?.max_segment_seconds ?? 60,
    audio_buffer_seconds: selected.config?.audio_buffer_seconds ?? 120,
    speech_pre_roll_seconds: selected.config?.speech_pre_roll_seconds ?? 0.3,
    progress_interval_seconds: selected.config?.progress_interval_seconds ?? 0.2,
    postprocessing: selected.config?.postprocessing ?? null,
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
      window.requestAnimationFrame(() => void fitView({ padding: 0.18, duration: 260, minZoom: 0.55, maxZoom: 1 }));
    } catch (reason) {
      setConnectionError(reason instanceof Error ? `自动排列失败：${reason.message}` : "自动排列失败，请稍后重试。");
    }
  }, [document, fitView, flowEdges, flowNodes, onChange, readPortOffsets, rerouteAfterRender]);

  useEffect(() => {
    if (!nodesInitialized || checkedInitialLayout.current) return;
    const expectedIds = new Set(["$input", "$outputs", ...document.nodes.map((node) => node.id)]);
    if (flowNodes.length !== expectedIds.size || flowNodes.some((node) => !expectedIds.has(node.id))) return;
    checkedInitialLayout.current = true;
    const positionedNodeIds = new Set(Object.keys(document.layout ?? {}));
    if (needsAutoLayout(flowNodes, document.nodes.map((node) => node.id), positionedNodeIds)) {
      const frame = window.requestAnimationFrame(() => void arrangeGraph());
      return () => window.cancelAnimationFrame(frame);
    }
  }, [arrangeGraph, document.layout, document.nodes, flowNodes, nodesInitialized]);

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

  const confirmTemplate = () => {
    if (!pendingTemplate) return;
    const next = applyAudioGraphTemplate(document, pendingTemplate, vectorSpaces, engines);
    setSelectedId(null);
    setConnectionError(null);
    setPendingTemplate(null);
    onChange(next);
    window.requestAnimationFrame(() => void fitView({ padding: 0.18, duration: 260, minZoom: 0.55, maxZoom: 1 }));
  };

  const onNodesChange = (changes: NodeChange<GraphNode>[]) =>
    setFlowNodes((current) => applyNodeChanges(changes, current));
  const onEdgesChange = (changes: EdgeChange<GraphEdge>[]) =>
    setFlowEdges((current) => applyEdgeChanges(changes, current));

  const focusedEdges = useMemo(() => {
    const hasFocus = Boolean(selectedId || selectedEdgeId);
    return flowEdges.map((edge) => {
      const related = selectedEdgeId === edge.id || (selectedId != null && (edge.source === selectedId || edge.target === selectedId));
      return { ...edge, className: `${edge.className ?? ""} ${hasFocus ? related ? "edge-focused" : "edge-dimmed" : ""}`.trim() };
    });
  }, [flowEdges, selectedEdgeId, selectedId]);

  const legendKinds = ["pcm_chunk", "audio_segment", "diarization_segment", "embedding", "vector_search_result", "diarization_local_result", "diarization_cluster_result", "speaker_transcription", "transcription", "speech_started", "speech_stopped"];
  return (
    <div className={`audio-builder ${sidebarOpen ? "palette-open" : "palette-closed"} ${selected ? "inspector-open" : ""}`}>
      <WorkbenchSidebar
        open={sidebarOpen}
        expandedTitle="工作流工具"
        collapsedTitle="工作流"
        ariaLabel="工作流侧栏"
        content={sidebarContent}
        footer={sidebarFooter}
        onOpenChange={onSidebarOpenChange}
      >
        <details className="audio-template-catalog" open={templatesOpen} onToggle={(event) => setTemplatesOpen(event.currentTarget.open)}>
          <summary><h2>快速模板</h2><span>自动连线</span><i aria-hidden="true">⌄</i></summary>
          <div className="audio-catalog-content">
            <p>生成完整处理链与命名出口，只需选择执行引擎。</p>
            <button onClick={() => setPendingTemplate("physical_asr")}><strong>物理 VAD + ASR</strong><small>PCM → 物理 VAD → ASR</small></button>
            <button onClick={() => setPendingTemplate("physical_semantic_asr")}><strong>物理 + 语义 VAD + ASR</strong><small>PCM → 物理 VAD → 语义 VAD → ASR</small></button>
            <button onClick={() => setPendingTemplate("dual_physical_asr")}><strong>物理双路 VAD + ASR</strong><small>正式转写 + AHA 中间结果预览</small></button>
            <button onClick={() => setPendingTemplate("dual_physical_semantic_asr")}><strong>双路物理 + 语义 VAD + ASR</strong><small>正式分支语义判停 + AHA 中间结果预览</small></button>
            <button onClick={() => setPendingTemplate("meeting_complete")}><strong>会议系统完全体</strong><small>双路语义判停、正式与预览转写、语音特征向量提取与搜索</small></button>
          </div>
        </details>
        <details className="audio-node-catalog" open={nodeCatalogOpen} onToggle={(event) => setNodeCatalogOpen(event.currentTarget.open)}>
          <summary><h2>节点目录</h2><span>{nodeTypes.length} 种</span><i aria-hidden="true">⌄</i></summary>
          <div className="audio-catalog-content">
            <p>添加类型化语音节点；选中节点后在右侧配置。</p>
            {nodeTypes.map((type) => (
              <button key={type.kind} onClick={() => addNode(type.kind)}>
                <strong>{type.display_name}</strong>
                <small>{type.inputs.map((port) => portMeta(port.kind).label).join(" + ") || "无输入"} → {type.outputs.map((port) => portMeta(port.kind).label).join(" + ") || "无输出"}</small>
              </button>
            ))}
          </div>
        </details>
      </WorkbenchSidebar>

      <section className="audio-canvas panel" aria-label="语音工作流画布">
        {connectionError && <div className="graph-toast">{connectionError}</div>}
        <ReactFlow
          nodes={flowNodes}
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
          connectionLineStyle={{ stroke: portMeta(connectingKind ?? "").color, strokeWidth: 2.25 }}
          isValidConnection={validConnection}
          onPaneClick={() => { setSelectedId(null); setSelectedEdgeId(null); }}
          onNodeClick={(_event, node) => { setSelectedEdgeId(null); setSelectedId(node.id.startsWith("$") ? null : node.id); }}
          onEdgeClick={(_event, edge) => { setSelectedId(null); setSelectedEdgeId(edge.id); }}
          onNodeDragStop={(_event, node) => {
            if (node.id.startsWith("$")) return;
            const nextNodes = flowNodes.map((item) => item.id === node.id ? { ...item, position: node.position } : item);
            rerouteAfterRender(nextNodes, flowEdges);
            onChange({ ...document, layout: { ...document.layout, [node.id]: node.position } });
          }}
          fitView
          fitViewOptions={{ padding: 0.18, minZoom: 0.55, maxZoom: 1 }}
          minZoom={0.35}
          maxZoom={1.6}
          deleteKeyCode={["Backspace", "Delete"]}
        >
          <Background gap={24} size={1} color="#d9dfdb" />
          <Panel position="top-center" className="audio-graph-legend">
            <div className="role-legend"><span><i style={{ background: ROLE_COLORS.input }} />输入</span><span><i style={{ background: ROLE_COLORS.processor }} />处理</span><span><i style={{ background: ROLE_COLORS.output }} />输出</span></div>
            <div className="type-legend">{legendKinds.map((kind) => <span key={kind}><i style={{ background: portMeta(kind).color }} />{portMeta(kind).label}</span>)}</div>
          </Panel>
          <MiniMap pannable zoomable nodeColor={(node) => ROLE_COLORS[node.data.role as keyof typeof ROLE_COLORS] ?? "#69716c"} />
          <Controls fitViewOptions={{ padding: 0.18, minZoom: 0.55, maxZoom: 1 }}>
            <ControlButton aria-label="自动排列" title="自动排列" onClick={() => void arrangeGraph()}>↔</ControlButton>
          </Controls>
        </ReactFlow>
      </section>

      {selected && selectedSpec && <aside className="audio-inspector panel" aria-label="节点配置面板">
        <header><div><span>节点配置</span><h2>{selected.id}</h2></div><button className="icon-button" aria-label="关闭节点配置" onClick={() => setSelectedId(null)}>×</button></header>
        <div className="audio-inspector-body">
          <div className={`inspector-state ${selected.engine_id || selectedSpec.required_api == null ? "configured" : "unconfigured"}`}><strong>{selectedSpec.display_name}</strong><span>{selectedSpec.required_api == null ? "基础设施节点" : selected.engine_id ? "已绑定执行引擎" : "待绑定执行引擎"}</span></div>
          <label><span>节点 ID</span><input value={selected.id} disabled /></label>
          {selectedSpec.required_api != null && <label><span>执行引擎</span><select value={selected.engine_id ?? ""} onChange={(event) => updateNode({ ...selected, engine_id: event.target.value || null } as AudioNode)}><option value="">未绑定</option>{engines.filter((engine) => engine.enabled && engine.api === selectedSpec.required_api && (!selectedSpec.required_capability || (Array.isArray(engine.details?.capabilities) && engine.details.capabilities.includes(selectedSpec.required_capability)))).map((engine) => <option key={engine.id} value={engine.id}>{engine.name} · {engine.status}</option>)}</select><small>需要 {selectedSpec.required_api}{selectedSpec.required_capability ? ` / ${selectedSpec.required_capability}` : ""} 能力</small></label>}
          {Object.entries(selected.config ?? {}).filter(([key]) => !["postprocessing", "parameters", "clustering_algorithm", "remove_punctuation", "normalize", "include_embedding", "filter", "collection_name", "vector_name", "progress_interval_seconds"].includes(key)).map(([key, value]) => <label key={key}><span>{key}</span><input type={typeof value === "number" || value === null ? "number" : "text"} step="0.1" value={value == null ? "" : String(value)} onChange={(event) => updateNode({ ...selected, config: { ...(selected.config ?? {}), [key]: typeof value === "number" || value === null ? (event.target.value ? Number(event.target.value) : null) : event.target.value } } as AudioNode)} /></label>)}
          {selected.kind === "audio_embedding" && <label className="switch-control"><input type="checkbox" checked={selected.config?.normalize ?? true} onChange={(event) => updateNode({ ...selected, config: { algorithm: selected.config?.algorithm ?? "eres2netv2", expected_dimension: selected.config?.expected_dimension ?? 192, normalize: event.target.checked } })} /><span>L2 归一化</span></label>}
          {selected.kind === "vector_search" && <><label><span>Voiceprint Collection</span><select value={selected.config.collection_name} onChange={(event) => { const space = vectorSpaces.find((item) => item.collection_name === event.target.value); if (space) updateNode({ ...selected, config: { ...selected.config, collection_name: space.collection_name, vector_name: space.vectors[0]?.name ?? "" } }); }}><option value="">请选择 voiceprint Collection</option>{vectorSpaces.filter((space) => space.template === "voiceprint").map((space) => <option value={space.collection_name} key={space.collection_name}>{space.collection_name}</option>)}</select></label><label><span>Named Vector</span><select value={selected.config.vector_name} onChange={(event) => updateNode({ ...selected, config: { ...selected.config, vector_name: event.target.value } })}>{vectorSpaces.find((item) => item.collection_name === selected.config.collection_name)?.vectors.map((vector) => <option key={vector.name}>{vector.name}</option>)}</select></label><label><span>Filter JSON</span><textarea value={JSON.stringify(selected.config.filter ?? {}, null, 2)} onChange={(event) => { try { updateNode({ ...selected, config: { ...selected.config, filter: JSON.parse(event.target.value) } }); } catch { /* 保存前由校验提示 */ } }} /></label><label className="switch-control"><input type="checkbox" checked={selected.config.include_embedding} onChange={(event) => updateNode({ ...selected, config: { ...selected.config, include_embedding: event.target.checked } })} /><span>在 AHA 事件中返回查询向量</span></label></>}
          {selected.kind === "asr" && <label className="switch-control"><input type="checkbox" checked={selected.config?.remove_punctuation ?? false} onChange={(event) => updateNode({ ...selected, config: { ...selected.config, remove_punctuation: event.target.checked } })} /><span>移除转写标点</span></label>}
          {selected.kind === "physical_vad" && selectedPhysicalConfig && <section className="vad-postprocessing-settings">
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
          {selected.kind === "speaker_clustering" && selectedClusteringConfig && <section className="clustering-parameters-settings">
            <label><span>聚类算法</span><select value={selectedClusteringConfig.clustering_algorithm} onChange={(event) => updateNode({ ...selected, config: { ...selectedClusteringConfig, clustering_algorithm: event.target.value as "hdbscan" } })}><option value="hdbscan">HDBSCAN</option></select></label>
            <div className="inspector-state configured"><strong>HDBSCAN 参数模板</strong><span>固定字段 · 严格校验</span></div>
            {HDBSCAN_NUMBER_PARAMETER_META.map(([key, label, help, min, step]) => <label key={key}><span>{label}</span><input type="number" min={min} step={step} value={selectedClusteringConfig.parameters[key]} onChange={(event) => updateNode({ ...selected, config: { ...selectedClusteringConfig, parameters: { ...selectedClusteringConfig.parameters, [key]: Number(event.target.value) } } })} /><small>{help}</small></label>)}
            <label className="switch-control"><input type="checkbox" checked={selectedClusteringConfig.parameters.allow_single_cluster} onChange={(event) => updateNode({ ...selected, config: { ...selectedClusteringConfig, parameters: { ...selectedClusteringConfig.parameters, allow_single_cluster: event.target.checked } } })} /><span>允许所有向量形成单一簇</span><small>开启后更容易把不同说话人融合到同一个簇。</small></label>
          </section>}
          {selected.kind === "semantic_vad" && <label><span>失败策略</span><select value={selected.failure_policy} onChange={(event) => updateNode({ ...selected, failure_policy: event.target.value as "fail_open" | "fail_pipeline" })}><option value="fail_open">fail_open</option><option value="fail_pipeline">fail_pipeline</option></select></label>}
          {selected.kind === "speaker_clustering" && <label><span>失败策略</span><select value={selected.failure_policy} onChange={(event) => updateNode({ ...selected, failure_policy: event.target.value as "fail_open" | "fail_pipeline" })}><option value="fail_open">fail_open</option><option value="fail_pipeline">fail_pipeline</option></select></label>}
          {diagnostics.filter((item) => item.node_id === selected.id).map((item, index) => <div className="inline-error" key={`${item.message}-${index}`}>{item.message}</div>)}
          <button className="danger-text inspector-delete" onClick={deleteSelected}>删除节点</button>
        </div>
      </aside>}
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
