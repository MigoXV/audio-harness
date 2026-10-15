import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Background,
  ControlButton,
  Controls,
  Handle,
  MiniMap,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useNodesInitialized,
  useReactFlow,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type {
  AgentBackend,
  AudioHarnessApi,
  AudioWorkflow,
  EngineAsset,
  VoiceAgentDefinition,
  VoiceAgentValidation,
} from "../api/types";
import {
  buildVoiceAgentGraph,
  VOICE_AGENT_GRAPH_COLORS,
  type VoiceAgentGraphNode,
  type VoiceAgentNodeId,
} from "../features/voiceAgents/voiceAgentGraphModel";
import { useVoiceAgentWorkflowInterface } from "../features/voiceAgents/useVoiceAgentWorkflowInterface";
import { AgentBackendBindingFields } from "./AgentBackendBindingFields";
import { EngineQuickView } from "./EngineQuickView";
import { EngineModelPicker } from "./EngineModelPicker";
import { PresetVoicePicker } from "./PresetVoicePicker";
import { SpeakerContextFields } from "./SpeakerContextFields";
import { VoiceAgentPromptTemplates } from "./VoiceAgentPromptTemplates";
import { WorkbenchSidebar } from "./WorkbenchSidebar";
import { vars } from "../styles/theme.css";
import { isCompactWorkbench, useInspectorFocus } from "./useInspectorFocus";
import { collectGraphPortOffsets, layoutGraph, type PortOffsets } from "./graphLayout";

interface Props {
  api: AudioHarnessApi;
  onEngineUpdated: (engine: EngineAsset) => void;
  definition: VoiceAgentDefinition;
  workflows: AudioWorkflow[];
  engines: EngineAsset[];
  backends: AgentBackend[];
  diagnostics: VoiceAgentValidation["diagnostics"];
  agentBackendContextPreview?: string | null;
  agentBackendContextPreviewStale?: boolean;
  speakerContextPreview?: string | null;
  speakerContextPreviewStale?: boolean;
  sidebarOpen: boolean;
  sidebarContent?: ReactNode;
  sidebarFooter?: ReactNode;
  onSidebarOpenChange: (open: boolean) => void;
  onChange: (definition: VoiceAgentDefinition) => void;
}

function AgentGraphNode({ data, selected }: NodeProps<VoiceAgentGraphNode>) {
  const stateLabel = data.stateLabel ?? ({
    boundary: null,
    configured: "已配置",
    unconfigured: "待配置",
    optional: "可选",
    invalid: "配置错误",
  } as const)[data.state];
  return <article
    aria-label={`节点 ${data.label}`}
    className={`audio-graph-node voice-agent-node state-${data.state} ${selected ? "selected" : ""}`}
    data-node-state={data.state}
  >
    <header className="audio-node-header">
      <div><span className="audio-node-kind">{data.subtitle}</span>{stateLabel && <span className="audio-node-state">{stateLabel}</span>}</div>
      <strong>{data.label}</strong>
      <small>{data.summary}</small>
    </header>
    <div className="audio-node-port-groups">
      {data.inputs.length > 0 && <section className="audio-port-group input-ports"><span>输入</span><ul>{data.inputs.map((port) => <li className="audio-port-row port-input" key={port.id}>
        <Handle id={port.id} type="target" position={Position.Left} style={{ top: "50%", background: port.color }} />
        <code>{port.label}</code>
      </li>)}</ul></section>}
      {data.outputs.length > 0 && <section className="audio-port-group output-ports"><span>输出</span><ul>{data.outputs.map((port) => <li className="audio-port-row port-output" key={port.id}>
        <Handle id={port.id} type="source" position={Position.Right} style={{ top: "50%", background: port.color }} />
        <code>{port.label}</code>
      </li>)}</ul></section>}
    </div>
  </article>;
}

const nodeTypes = { agent: AgentGraphNode };

export function VoiceAgentGraphEditor(props: Props) {
  return <ReactFlowProvider><VoiceAgentGraphEditorInner {...props} /></ReactFlowProvider>;
}

function VoiceAgentGraphEditorInner({
  api, onEngineUpdated, definition, workflows, engines, backends, diagnostics, agentBackendContextPreview, agentBackendContextPreviewStale, speakerContextPreview, speakerContextPreviewStale,
  sidebarOpen, sidebarContent, sidebarFooter, onSidebarOpenChange, onChange,
}: Props) {
  const workflowInterface = useVoiceAgentWorkflowInterface(api, definition.audio_workflow_id);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [selectedId, setSelectedId] = useState<VoiceAgentNodeId | null>(null);
  const focusSidebar = useInspectorFocus(selectedId);
  const [layoutError, setLayoutError] = useState<string | null>(null);
  const nodesInitialized = useNodesInitialized();
  const lastAutoLayoutSignature = useRef("");
  const { fitView, getInternalNode } = useReactFlow<VoiceAgentGraphNode>();
  const graph = useMemo(() => buildVoiceAgentGraph({
    definition,
    workflows,
    engines,
    backends,
    diagnostics,
  }), [backends, definition, diagnostics, engines, workflows]);
  const freshNodes = graph.nodes;
  const edges = graph.edges;
  const nodes = useMemo(() => freshNodes.map((node) => ({
    ...node,
    position: positions[node.id] ?? node.position,
    selected: node.id === selectedId,
  })), [freshNodes, positions, selectedId]);

  const readPortOffsets = useCallback((values: VoiceAgentGraphNode[]): PortOffsets => {
    return collectGraphPortOffsets(
      values.map((node) => node.id),
      (nodeId) => getInternalNode(nodeId)?.internals.handleBounds,
    );
  }, [getInternalNode]);

  const arrangeGraph = useCallback(async () => {
    try {
      const layouted = await layoutGraph(nodes, edges, readPortOffsets(nodes), {
        graphId: "voice-agent",
        firstNodeIds: ["$input"],
        lastNodeIds: ["$output"],
      });
      setPositions(Object.fromEntries(layouted.map((node) => [node.id, node.position])));
      setLayoutError(null);
      window.requestAnimationFrame(() => void fitView({ padding: 0.14, duration: 260, minZoom: 0.45, maxZoom: 1 }));
    } catch (reason) {
      setLayoutError(reason instanceof Error ? `自动排列失败：${reason.message}` : "自动排列失败，请稍后重试。");
    }
  }, [edges, fitView, nodes, readPortOffsets]);

  useEffect(() => {
    if (!nodesInitialized) return;
    const signature = `${nodes.map((node) => node.id).join("|")}::${edges.map((edge) => edge.id).join("|")}`;
    if (lastAutoLayoutSignature.current === signature) return;
    lastAutoLayoutSignature.current = signature;
    const frame = window.requestAnimationFrame(() => void arrangeGraph());
    return () => window.cancelAnimationFrame(frame);
  }, [arrangeGraph, edges, nodes, nodesInitialized]);

  const responsesEngines = engines.filter((item) => item.enabled && item.api === "responses");
  const ttsEngines = engines.filter((item) => item.enabled && item.api === "qwen_tts_duplex");
  const selectedResponsesEngine = responsesEngines.find((item) => item.id === definition.responses_engine_id);
  const selectedTtsEngine = ttsEngines.find((item) => item.id === definition.tts_engine_id);
  const responseConfig = definition.responses!;
  const ttsConfig = definition.tts!;
  const voices = ttsConfig.allowed_voices ?? [];
  const removeVoice = (value: string) => {
    const next = voices.filter((item) => item !== value);
    onChange({ ...definition, tts: { ...ttsConfig, allowed_voices: next, default_voice: next.includes(ttsConfig.default_voice) ? ttsConfig.default_voice : next[0] ?? "" } });
  };
  const selectedDiagnostics = diagnostics.filter((item) => {
    if (selectedId === "workflow") return item.field?.startsWith("audio_workflow") || item.field?.startsWith("speaker_context");
    if (selectedId === "responses") return item.field?.startsWith("responses");
    if (selectedId === "tts") return item.field?.startsWith("tts");
    if (selectedId === "backend") return item.field?.startsWith("agent_backend");
    return false;
  });

  return <div className={`audio-builder voice-agent-builder ${sidebarOpen ? "palette-open" : "palette-closed"}`}>
    <WorkbenchSidebar open={sidebarOpen} expandedTitle="智能体工具" collapsedTitle="智能体" ariaLabel="语音智能体侧栏" content={sidebarContent} footer={sidebarFooter} onOpenChange={(open) => { if (open && isCompactWorkbench()) { setSelectedId(null); focusSidebar(); } onSidebarOpenChange(open); }} />
    <section className="audio-canvas panel" aria-label="语音智能体构建画布">
      {layoutError && <div className="graph-toast">{layoutError}</div>}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={(changes) => {
          setPositions((current) => {
            const next = { ...current };
            let changed = false;
            changes.forEach((change) => {
              if (change.type === "position" && change.position) {
                next[change.id] = change.position;
                changed = true;
              }
            });
            return changed ? next : current;
          });
        }}
        onNodeClick={(_, node) => {
          if (["workflow", "responses", "backend", "tts"].includes(node.id)) { setSelectedId(node.id as VoiceAgentNodeId); if (isCompactWorkbench()) onSidebarOpenChange(false); }
        }}
        nodesConnectable={false}
        edgesFocusable={false}
        edgesReconnectable={false}
        deleteKeyCode={null}
        fitView
        fitViewOptions={{ padding: 0.12 }}
        minZoom={0.35}
        maxZoom={1.5}
      >
        <Background gap={24} size={1} color={vars.color.line} />
        <Controls showInteractive={false}><ControlButton aria-label="自动排列" title="自动排列" onClick={() => void arrangeGraph()}>↔</ControlButton></Controls>
        <MiniMap pannable zoomable nodeColor={(node) => node.id === "$input" ? VOICE_AGENT_GRAPH_COLORS.audio : node.id === "$output" ? VOICE_AGENT_GRAPH_COLORS.speech : VOICE_AGENT_GRAPH_COLORS.text} maskColor={vars.color.overlay} />
        <Panel position="top-center" className="voice-agent-graph-notice"><strong>固定拓扑</strong><span>语音工作流作为单个只读引用节点展示；整图可自动排列，暂不支持增删节点或重连。</span></Panel>
      </ReactFlow>
      {selectedId && <aside className="audio-inspector panel" aria-label="智能体节点配置面板" onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); setSelectedId(null); } }}>
        <header><div><span>节点配置</span><h2>{selectedId === "workflow" ? "语音工作流" : nodes.find((item) => item.id === selectedId)?.data.label}</h2></div><button className="icon-button" aria-label="关闭节点配置" onClick={() => setSelectedId(null)}>×</button></header>
        <div className="audio-inspector-body">
          {selectedId === "workflow" && <div className="agent-node-fields"><label><span>语音工作流</span><select aria-label="语音工作流" value={definition.audio_workflow_id} onChange={(event) => onChange({ ...definition, audio_workflow_id: event.target.value })}><option value="">未绑定</option>{workflows.filter((item) => item.active_revision !== null).map((workflow) => <option key={workflow.id} value={workflow.id}>{workflow.name} · v{workflow.active_revision}</option>)}</select><small>仅引用已发布版本；后续发布不会热改当前智能体版本。</small></label><SpeakerContextFields definition={definition} workflowInterface={workflowInterface} contextPreview={speakerContextPreview} contextPreviewStale={speakerContextPreviewStale} onChange={onChange} /></div>}
          {selectedId === "responses" && <div className="agent-node-fields"><label><span>Responses 引擎</span><select value={definition.responses_engine_id ?? ""} onChange={(event) => onChange({ ...definition, responses_engine_id: event.target.value || null })}><option value="">未绑定</option>{definition.responses_engine_id && !responsesEngines.some((engine) => engine.id === definition.responses_engine_id) && <option value={definition.responses_engine_id} disabled>{engines.find((engine) => engine.id === definition.responses_engine_id)?.name ?? definition.responses_engine_id} · 当前不可用</option>}{responsesEngines.map((engine) => <option key={engine.id} value={engine.id}>{engine.name} · {engine.status}</option>)}</select></label><EngineQuickView api={api} engineId={definition.responses_engine_id} onUpdated={onEngineUpdated} /><EngineModelPicker api={api} engine={selectedResponsesEngine} value={responseConfig.model} onChange={(model) => onChange({ ...definition, responses: { ...responseConfig, model } })} /><VoiceAgentPromptTemplates currentInstructions={responseConfig.instructions} onApply={(instructions) => onChange({ ...definition, responses: { ...responseConfig, instructions } })} /><label><span>系统提示词</span><textarea value={responseConfig.instructions} onChange={(event) => onChange({ ...definition, responses: { ...responseConfig, instructions: event.target.value } })} /></label></div>}
          {selectedId === "tts" && <div className="agent-node-fields"><label><span>TTS 引擎</span><select value={definition.tts_engine_id ?? ""} onChange={(event) => onChange({ ...definition, tts_engine_id: event.target.value || null })}><option value="">未绑定</option>{definition.tts_engine_id && !ttsEngines.some((engine) => engine.id === definition.tts_engine_id) && <option value={definition.tts_engine_id} disabled>{engines.find((engine) => engine.id === definition.tts_engine_id)?.name ?? definition.tts_engine_id} · 当前不可用</option>}{ttsEngines.map((engine) => <option key={engine.id} value={engine.id}>{engine.name} · {engine.status}</option>)}</select></label><EngineQuickView api={api} engineId={definition.tts_engine_id} onUpdated={onEngineUpdated} /><label><span>默认音色</span><select value={ttsConfig.default_voice} onChange={(event) => onChange({ ...definition, tts: { ...ttsConfig, default_voice: event.target.value } })}><option value="">请选择</option>{voices.map((voice) => <option key={voice}>{voice}</option>)}</select></label><div className="agent-node-fields"><div className="tag-editor"><div>{voices.map((voice) => <button type="button" key={voice} onClick={() => removeVoice(voice)}>{voice}<span>×</span></button>)}</div></div><PresetVoicePicker api={api} engine={selectedTtsEngine} onAdd={(value) => { if (voices.includes(value)) return; onChange({ ...definition, tts: { ...ttsConfig, allowed_voices: [...voices, value], default_voice: ttsConfig.default_voice || value } }); }} /></div></div>}
          {selectedId === "backend" && <AgentBackendBindingFields api={api} backends={backends} definition={definition} contextPreview={agentBackendContextPreview} contextPreviewStale={agentBackendContextPreviewStale} onChange={onChange} />}
          {selectedDiagnostics.map((item) => <div className="inline-error" key={item.code}>{item.message}</div>)}
        </div>
      </aside>}
    </section>
  </div>;
}
