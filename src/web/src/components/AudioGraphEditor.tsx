import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  Background,
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
  type Connection,
  type Edge,
  type EdgeChange,
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
import { layoutAudioGraph, needsAutoLayout } from "./audioGraphLayout";
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
  inputs: Array<{ name: string; kind: string }>;
  outputs: Array<{ name: string; kind: string }>;
  role: "input" | "processor" | "output";
  state: "boundary" | "configured" | "unconfigured" | "invalid";
}

type GraphNode = Node<GraphNodeData>;

const OUTPUTS = [
  { name: "primary_transcript", kind: "transcription" },
  { name: "interim_transcript", kind: "transcription" },
  { name: "speech_started", kind: "speech_started" },
  { name: "speech_stopped", kind: "speech_stopped" },
  { name: "turn_progress", kind: "transcription" },
  { name: "vector_search", kind: "vector_search_result" },
];

const PORT_META: Record<string, { label: string; color: string }> = {
  pcm_chunk: { label: "PCM", color: "#2563eb" },
  audio_segment: { label: "音频片段", color: "#c2410c" },
  transcription: { label: "转写", color: "#7c3aed" },
  speech_started: { label: "开始事件", color: "#0f766e" },
  speech_stopped: { label: "停止事件", color: "#be123c" },
  embedding: { label: "Embedding", color: "#9a6700" },
  vector_search_result: { label: "检索结果", color: "#b45309" },
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

const FIRERED_PARAMETER_META = [
  ["smooth_window_size", "概率平滑窗口", "帧", 1],
  ["speech_threshold", "语音判定阈值", "0–1", 0.01],
  ["pad_start_frame", "语音起点前补", "帧", 1],
  ["min_speech_frame", "最短连续语音", "帧", 1],
  ["max_speech_frame", "单段最长语音", "帧", 1],
  ["min_silence_frame", "结束所需静音", "帧", 1],
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

function PortRow({ port, direction }: { port: { name: string; kind: string }; direction: "input" | "output" }) {
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
    <code style={{ color: meta.color }}>{meta.label}</code>
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
            outputs: spec?.outputs ?? [],
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
          inputs: OUTPUTS,
          outputs: [],
          role: "output",
          state: "boundary",
        },
      },
    ];
    return values;
  }, [diagnostics, document.edges, document.layout, document.nodes, document.outputs, specs]);
  const initialEdges = useMemo<Edge[]>(() => {
    const outputKind = (nodeId: string, portName: string): string => {
      const node = document.nodes.find((item) => item.id === nodeId);
      return specs.get(node?.kind ?? "")?.outputs.find((port) => port.name === portName)?.kind ?? "unknown";
    };
    const edgeStyle = (kind: string) => {
      const color = portMeta(kind).color;
      return { type: "smoothstep", className: `audio-edge edge-${kind}`, style: { stroke: color, strokeWidth: 2.25 }, markerEnd: { type: MarkerType.ArrowClosed, color } };
    };
    return [
      ...document.edges.map((edge) => ({
        id: `edge:${edge.id}`,
        source: edge.source.kind === "input" ? "$input" : edge.source.node_id,
        sourceHandle: edge.source.kind === "input" ? edge.source.name : edge.source.port,
        target: edge.target.node_id,
        targetHandle: edge.target.port,
        ...edgeStyle(edge.source.kind === "input" ? "pcm_chunk" : outputKind(edge.source.node_id, edge.source.port)),
      })),
      ...document.outputs.map((output) => ({
        id: `output:${output.name}`,
        source: output.source.node_id,
        sourceHandle: output.source.port,
        target: "$outputs",
        targetHandle: output.name,
        ...edgeStyle(outputKind(output.source.node_id, output.source.port)),
      })),
    ];
  }, [document.edges, document.nodes, document.outputs, specs]);
  const [flowNodes, setFlowNodes] = useState(initialNodes);
  const [flowEdges, setFlowEdges] = useState(initialEdges);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [connectingKind, setConnectingKind] = useState<string | null>(null);
  const [pendingTemplate, setPendingTemplate] = useState<AudioGraphTemplateId | null>(null);
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [nodeCatalogOpen, setNodeCatalogOpen] = useState(false);
  const nodesInitialized = useNodesInitialized();
  const { fitView } = useReactFlow<GraphNode, Edge>();
  const checkedInitialLayout = useRef(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setFlowNodes(initialNodes), 0);
    return () => window.clearTimeout(timer);
  }, [initialNodes]);
  useEffect(() => {
    const timer = window.setTimeout(() => setFlowEdges(initialEdges), 0);
    return () => window.clearTimeout(timer);
  }, [initialEdges]);

  const selected = document.nodes.find((node) => node.id === selectedId) ?? null;
  const selectedSpec = selected ? specs.get(selected.kind) : undefined;
  const selectedPhysicalConfig = selected?.kind === "physical_vad" ? {
    min_segment_seconds: selected.config?.min_segment_seconds ?? 0.5,
    max_segment_seconds: selected.config?.max_segment_seconds ?? 60,
    audio_buffer_seconds: selected.config?.audio_buffer_seconds ?? 120,
    speech_pre_roll_seconds: selected.config?.speech_pre_roll_seconds ?? 0.3,
    postprocessing: selected.config?.postprocessing ?? null,
  } : null;
  const selectedPostprocessing = selectedPhysicalConfig?.postprocessing ? {
    ...selectedPhysicalConfig.postprocessing,
    parameters: {
      ...FIRERED_POSTPROCESSING_DEFAULTS.parameters,
      ...(selectedPhysicalConfig.postprocessing.parameters ?? {}),
    },
  } : null;

  const arrangeGraph = useCallback(() => {
    try {
      const layoutedNodes = layoutAudioGraph(flowNodes, flowEdges);
      const layout = Object.fromEntries(
        layoutedNodes
          .filter((node) => node.data.role === "processor")
          .map((node) => [node.id, node.position]),
      );
      setFlowNodes(layoutedNodes);
      setConnectionError(null);
      onChange({ ...document, layout });
      window.requestAnimationFrame(() => void fitView({ padding: 0.18, duration: 260, minZoom: 0.55, maxZoom: 1 }));
    } catch (reason) {
      setConnectionError(reason instanceof Error ? `自动排列失败：${reason.message}` : "自动排列失败，请稍后重试。");
    }
  }, [document, fitView, flowEdges, flowNodes, onChange]);

  useEffect(() => {
    if (!nodesInitialized || checkedInitialLayout.current) return;
    const expectedIds = new Set(["$input", "$outputs", ...document.nodes.map((node) => node.id)]);
    if (flowNodes.length !== expectedIds.size || flowNodes.some((node) => !expectedIds.has(node.id))) return;
    checkedInitialLayout.current = true;
    const positionedNodeIds = new Set(Object.keys(document.layout ?? {}));
    if (needsAutoLayout(flowNodes, document.nodes.map((node) => node.id), positionedNodeIds)) {
      const frame = window.requestAnimationFrame(arrangeGraph);
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
    if (!source || !target || source !== target) return false;
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
    const next = applyAudioGraphTemplate(document, pendingTemplate, vectorSpaces);
    setSelectedId(null);
    setConnectionError(null);
    setPendingTemplate(null);
    onChange(next);
    window.requestAnimationFrame(() => void fitView({ padding: 0.18, duration: 260, minZoom: 0.55, maxZoom: 1 }));
  };

  const onNodesChange = (changes: NodeChange<GraphNode>[]) =>
    setFlowNodes((current) => applyNodeChanges(changes, current));
  const onEdgesChange = (changes: EdgeChange<Edge>[]) =>
    setFlowEdges((current) => applyEdgeChanges(changes, current));

  const legendKinds = ["pcm_chunk", "audio_segment", "embedding", "vector_search_result", "transcription", "speech_started", "speech_stopped"];
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
          edges={flowEdges}
          nodeTypes={flowNodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onEdgesDelete={removeEdges}
          onConnect={connect}
          onConnectStart={(_event, params) => setConnectingKind(portKind(params.nodeId ?? "", params.handleId, "out") ?? null)}
          onConnectEnd={() => setConnectingKind(null)}
          connectionLineType={ConnectionLineType.SmoothStep}
          connectionLineStyle={{ stroke: portMeta(connectingKind ?? "").color, strokeWidth: 2.25 }}
          isValidConnection={validConnection}
          onPaneClick={() => setSelectedId(null)}
          onNodeClick={(_event, node) => setSelectedId(node.id.startsWith("$") ? null : node.id)}
          onNodeDragStop={(_event, node) => {
            if (node.id.startsWith("$")) return;
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
            <ControlButton aria-label="自动排列" title="自动排列" onClick={arrangeGraph}>↔</ControlButton>
          </Controls>
        </ReactFlow>
      </section>

      {selected && selectedSpec && <aside className="audio-inspector panel" aria-label="节点配置面板">
        <header><div><span>节点配置</span><h2>{selected.id}</h2></div><button className="icon-button" aria-label="关闭节点配置" onClick={() => setSelectedId(null)}>×</button></header>
        <div className="audio-inspector-body">
          <div className={`inspector-state ${selected.engine_id || selectedSpec.required_api == null ? "configured" : "unconfigured"}`}><strong>{selectedSpec.display_name}</strong><span>{selectedSpec.required_api == null ? "基础设施节点" : selected.engine_id ? "已绑定执行引擎" : "待绑定执行引擎"}</span></div>
          <label><span>节点 ID</span><input value={selected.id} disabled /></label>
          {selectedSpec.required_api != null && <label><span>执行引擎</span><select value={selected.engine_id ?? ""} onChange={(event) => updateNode({ ...selected, engine_id: event.target.value || null } as AudioNode)}><option value="">未绑定</option>{engines.filter((engine) => engine.enabled && engine.api === selectedSpec.required_api).map((engine) => <option key={engine.id} value={engine.id}>{engine.name} · {engine.status}</option>)}</select><small>需要 {selectedSpec.required_api} 能力</small></label>}
          {Object.entries(selected.config ?? {}).filter(([key]) => !["postprocessing", "remove_punctuation", "normalize", "include_embedding", "filter", "collection_name", "vector_name"].includes(key)).map(([key, value]) => <label key={key}><span>{key}</span><input type={typeof value === "number" || value === null ? "number" : "text"} step="0.1" value={value == null ? "" : String(value)} onChange={(event) => updateNode({ ...selected, config: { ...(selected.config ?? {}), [key]: typeof value === "number" || value === null ? (event.target.value ? Number(event.target.value) : null) : event.target.value } } as AudioNode)} /></label>)}
          {selected.kind === "audio_embedding" && <label className="switch-control"><input type="checkbox" checked={selected.config?.normalize ?? true} onChange={(event) => updateNode({ ...selected, config: { algorithm: selected.config?.algorithm ?? "eres2netv2", expected_dimension: selected.config?.expected_dimension ?? 192, normalize: event.target.checked } })} /><span>L2 归一化</span></label>}
          {selected.kind === "vector_search" && <><label><span>Voiceprint Collection</span><select value={selected.config.collection_name} onChange={(event) => { const space = vectorSpaces.find((item) => item.collection_name === event.target.value); if (space) updateNode({ ...selected, config: { ...selected.config, collection_name: space.collection_name, vector_name: space.vectors[0]?.name ?? "" } }); }}><option value="">请选择 voiceprint Collection</option>{vectorSpaces.filter((space) => space.template === "voiceprint").map((space) => <option value={space.collection_name} key={space.collection_name}>{space.collection_name}</option>)}</select></label><label><span>Named Vector</span><select value={selected.config.vector_name} onChange={(event) => updateNode({ ...selected, config: { ...selected.config, vector_name: event.target.value } })}>{vectorSpaces.find((item) => item.collection_name === selected.config.collection_name)?.vectors.map((vector) => <option key={vector.name}>{vector.name}</option>)}</select></label><label><span>Filter JSON</span><textarea value={JSON.stringify(selected.config.filter ?? {}, null, 2)} onChange={(event) => { try { updateNode({ ...selected, config: { ...selected.config, filter: JSON.parse(event.target.value) } }); } catch { /* 保存前由校验提示 */ } }} /></label><label className="switch-control"><input type="checkbox" checked={selected.config.include_embedding} onChange={(event) => updateNode({ ...selected, config: { ...selected.config, include_embedding: event.target.checked } })} /><span>在 AHA 事件中返回查询向量</span></label></>}
          {selected.kind === "asr" && <label className="switch-control"><input type="checkbox" checked={selected.config?.remove_punctuation ?? false} onChange={(event) => updateNode({ ...selected, config: { ...selected.config, remove_punctuation: event.target.checked } })} /><span>移除转写标点</span></label>}
          {selected.kind === "physical_vad" && selectedPhysicalConfig && <section className="vad-postprocessing-settings">
            <label className="switch-control"><input type="checkbox" checked={selectedPostprocessing != null} onChange={(event) => updateNode({ ...selected, config: { ...selectedPhysicalConfig, postprocessing: event.target.checked ? structuredClone(FIRERED_POSTPROCESSING_DEFAULTS) : null } })} /><span>自定义 FireRed 后处理</span></label>
            {selectedPostprocessing && <>
              <div className="inspector-state configured"><strong>firered.stream_vad</strong><span>协议版本 v1 · 1 帧 = 10 ms</span></div>
              {FIRERED_PARAMETER_META.map(([key, label, unit, step]) => <label key={key}><span>{label}</span><input type="number" min={key === "speech_threshold" ? 0 : 1} max={key === "speech_threshold" ? 1 : undefined} step={step} value={selectedPostprocessing.parameters[key]} onChange={(event) => updateNode({ ...selected, config: { ...selectedPhysicalConfig, postprocessing: { ...selectedPostprocessing, parameters: { ...selectedPostprocessing.parameters, [key]: Number(event.target.value) } } } })} /><small>{unit}</small></label>)}
            </>}
          </section>}
          {selected.kind === "semantic_vad" && <label><span>失败策略</span><select value={selected.failure_policy} onChange={(event) => updateNode({ ...selected, failure_policy: event.target.value as "fail_open" | "fail_pipeline" })}><option value="fail_open">fail_open</option><option value="fail_pipeline">fail_pipeline</option></select></label>}
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
