import * as Tabs from "@radix-ui/react-tabs";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ControlButton,
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useNodesInitialized,
  useNodeConnections,
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
import { isCompactWorkbench, useInspectorFocus } from "./useInspectorFocus";
import { vars } from "../styles/theme.css";
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
  toolbarContent?: ReactNode;
  onSidebarOpenChange: (open: boolean) => void;
  onChange: (definition: VoiceAgentDefinition) => void;
}

const agentAssets: Record<string, { glyph: string; small: string }> = {
  "$input": { glyph: "imgGlyph", small: "imgGlyph6" }, workflow: { glyph: "imgGlyph1", small: "imgGlyph7" },
  responses: { glyph: "imgGlyph2", small: "imgGlyph8" }, tts: { glyph: "imgGlyph3", small: "imgGlyph9" },
  "$output": { glyph: "imgGlyph4", small: "imgGlyph10" }, backend: { glyph: "imgGlyph5", small: "imgGlyph11" },
};
function AgentIcon({ id, small = false }: { id: string; small?: boolean }) {
  return <span className={small ? "agent-node-glyph agent-node-glyph-small" : "agent-node-glyph"} aria-hidden="true" style={{ maskImage: `url(/figma-sync/agent-build-${agentAssets[id][small ? "small" : "glyph"]}.svg)` }} />;
}
function AgentPort({ nodeId, portId }: { nodeId: string; portId: string }) {
  const asset = portId === "audio" ? "imgPortOutPcm" : portId === "speech" ? "imgPortOutAudio" : ["result", "delegate"].includes(portId) ? "imgPortInDelegateResult" : "imgPortOutTranscript";
  const color = portId === "audio" ? vars.color.portAudio : portId === "speech" ? vars.color.portSegment : vars.color.textMuted;
  const mask = `url(/figma-sync/agent-build-${asset}.svg)`;
  return <span className="agent-port-asset" aria-hidden="true" data-port-kind={`${nodeId}.${portId}`} style={{ background: color, maskImage: `${mask}, ${mask}`, maskSize: "100% 100%, 62.5% 62.5%", maskComposite: "exclude" }} />;
}
function AgentGraphNode({ id, data, selected }: NodeProps<VoiceAgentGraphNode>) {
  const inputs = useNodeConnections({ handleType: "target" });
  const outputs = useNodeConnections({ handleType: "source" });
  const rows = Math.max(data.inputs.length, data.outputs.length);
  return <article aria-label={`节点 ${data.label}`} className={`agent-graph-node state-${data.state} ${selected ? "selected" : ""}`} data-node-state={data.state}>
    <header className="agent-node-header"><AgentIcon id={id} /><strong>{data.subtitle}</strong>{data.state === "invalid" ? <small role="status">配置错误</small> : data.state === "unconfigured" ? <small>待配置</small> : id === "workflow" ? <small>只读引用</small> : null}</header>
    <p className="agent-node-summary">{data.summary}</p>
    <div className="agent-node-ports">{Array.from({ length: rows }, (_, index) => <div className="agent-port-pair" key={index}>{(["inputs", "outputs"] as const).map(direction => {
      const port = data[direction][index];
      const connected = port && (direction === "inputs" ? inputs.some(connection => connection.targetHandle === port.id) : outputs.some(connection => connection.sourceHandle === port.id));
      return <div className={`agent-port-cell ${direction}`} key={direction}>{port && <><Handle id={port.id} type={direction === "inputs" ? "target" : "source"} position={direction === "inputs" ? Position.Left : Position.Right} title={`${port.label}${connected ? " · 已连接" : " · 未连接"}`}><AgentPort nodeId={id} portId={port.id} /></Handle><span>{port.label}</span></>}</div>;
    })}</div>)}</div>
  </article>;
}

const nodeTypes = { agent: AgentGraphNode };
const libraryOrder = ["$input", "workflow", "responses", "tts", "$output", "backend"];

export function VoiceAgentGraphEditor(props: Props) {
  return <ReactFlowProvider><VoiceAgentGraphEditorInner {...props} /></ReactFlowProvider>;
}

function VoiceAgentGraphEditorInner({
  api, onEngineUpdated, definition, workflows, engines, backends, diagnostics, agentBackendContextPreview, agentBackendContextPreviewStale, speakerContextPreview, speakerContextPreviewStale,
  sidebarOpen, sidebarContent, sidebarFooter, toolbarContent, onSidebarOpenChange, onChange,
}: Props) {
  const workflowInterface = useVoiceAgentWorkflowInterface(api, definition.audio_workflow_id);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [selectedId, setSelectedId] = useState<VoiceAgentNodeId | null>(null);
  const [inspectorTab, setInspectorTab] = useState("basic");
  const focusSidebar = useInspectorFocus(selectedId);
  const selectNode = (id: string) => { setSelectedId(id as VoiceAgentNodeId); if (isCompactWorkbench()) onSidebarOpenChange(false); };
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

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => void fitView({ padding: .12, minZoom: .35, maxZoom: 1 }));
    return () => window.cancelAnimationFrame(frame);
  }, [fitView, sidebarOpen]);

  const selectedNode = nodes.find(node => node.id === selectedId);
  return <div className={`audio-builder voice-agent-builder agent-workbench ${selectedId ? "agent-inspector-open" : ""} ${sidebarOpen ? "palette-open" : "palette-closed"}`}>
    <WorkbenchSidebar open={sidebarOpen} expandedTitle="智能体工具" collapsedTitle="智能体" ariaLabel="语音智能体侧栏" content={<div className="agent-sidebar-content"><div className="agent-object-context">{sidebarContent}</div><section className="agent-node-library"><h2>节点</h2><p>固定拓扑 · {nodes.length} 个节点</p><div className="agent-library-scroll" tabIndex={0} role="region" aria-label="智能体节点目录">{[...nodes].sort((a, b) => libraryOrder.indexOf(a.id) - libraryOrder.indexOf(b.id)).map(node => <button type="button" key={node.id} aria-pressed={selectedId === node.id} onClick={() => selectNode(node.id)}><AgentIcon id={node.id} small />{node.data.subtitle}</button>)}</div></section></div>} footer={sidebarFooter} onOpenChange={(open) => { if (open && isCompactWorkbench()) { setSelectedId(null); focusSidebar(); } onSidebarOpenChange(open); }} />
    <section className="agent-stage">
      <header className="agent-toolbar">
        {sidebarOpen && <button className="icon-button" aria-label="收起语音智能体侧栏" onClick={() => onSidebarOpenChange(false)}>‹</button>}
        {toolbarContent}<p className="agent-canvas-summary">固定拓扑 · {nodes.length} 个节点</p>
      </header>
      <div className="agent-type-legend" aria-label="智能体端口类型"><span className="agent-type-audio">连续音频</span><span className="agent-type-speech">合成音频块</span><span className="agent-type-text">文本 / 结果</span><span className="agent-type-unknown">接口类型待确认</span></div>
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
            selectNode(node.id);
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
          <Controls showInteractive={false}><ControlButton aria-label="自动排列" title="自动排列" onClick={() => void arrangeGraph()}>↔</ControlButton></Controls>
        </ReactFlow>
      </section>
    </section>
        {selectedId && selectedNode && <aside className="audio-inspector panel" aria-label="智能体节点配置面板" onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); setSelectedId(null); } }}>
          <header><AgentIcon id={selectedId} /><h2>{selectedNode.data.subtitle}</h2><code>{selectedNode.data.label}</code><button className="icon-button agent-inspector-close" aria-label="关闭节点配置" onClick={() => setSelectedId(null)}><span aria-hidden="true" /></button></header>
          <Tabs.Root className="agent-inspector-tabs" value={inspectorTab} onValueChange={setInspectorTab}>
          <Tabs.List className="agent-inspector-tab-list" aria-label="智能体节点属性"><Tabs.Trigger value="basic">基础配置</Tabs.Trigger><Tabs.Trigger value="ports">输入与输出</Tabs.Trigger></Tabs.List>
          <Tabs.Content value="basic" className="audio-inspector-body">
            {selectedId === "workflow" && <div className="agent-node-fields"><label><span>语音工作流</span><select aria-label="语音工作流" value={definition.audio_workflow_id} onChange={(event) => onChange({ ...definition, audio_workflow_id: event.target.value })}><option value="">未绑定</option>{workflows.filter((item) => item.active_revision !== null).map((workflow) => <option key={workflow.id} value={workflow.id}>{workflow.name} · v{workflow.active_revision}</option>)}</select><small>仅引用已发布版本；后续发布不会热改当前智能体版本。</small></label><SpeakerContextFields definition={definition} workflowInterface={workflowInterface} contextPreview={speakerContextPreview} contextPreviewStale={speakerContextPreviewStale} onChange={onChange} /></div>}
            {selectedId === "responses" && <div className="agent-node-fields agent-response-fields"><label><span>执行引擎</span><select aria-label="Responses 引擎" value={definition.responses_engine_id ?? ""} onChange={(event) => onChange({ ...definition, responses_engine_id: event.target.value || null })}><option value="">未绑定</option>{definition.responses_engine_id && !responsesEngines.some((engine) => engine.id === definition.responses_engine_id) && <option value={definition.responses_engine_id} disabled>{engines.find((engine) => engine.id === definition.responses_engine_id)?.name ?? definition.responses_engine_id} · 当前不可用</option>}{responsesEngines.map((engine) => <option key={engine.id} value={engine.id}>{engine.name} · {engine.status}</option>)}</select></label><div className="agent-model-fields"><EngineQuickView api={api} engineId={definition.responses_engine_id} onUpdated={onEngineUpdated} /><EngineModelPicker label="模型" api={api} engine={selectedResponsesEngine} value={responseConfig.model} onChange={(model) => onChange({ ...definition, responses: { ...responseConfig, model } })} /></div><VoiceAgentPromptTemplates currentInstructions={responseConfig.instructions} onApply={(instructions) => onChange({ ...definition, responses: { ...responseConfig, instructions } })} /><label><span>系统提示词</span><textarea aria-label="系统提示词" value={responseConfig.instructions} onChange={(event) => onChange({ ...definition, responses: { ...responseConfig, instructions: event.target.value } })} /></label><p className="agent-response-note">声纹身份规则在「语音工作流」节点内单独配置；这里仅定义通用回复策略。</p></div>}
            {selectedId === "tts" && <div className="agent-node-fields"><label><span>TTS 引擎</span><select value={definition.tts_engine_id ?? ""} onChange={(event) => onChange({ ...definition, tts_engine_id: event.target.value || null })}><option value="">未绑定</option>{definition.tts_engine_id && !ttsEngines.some((engine) => engine.id === definition.tts_engine_id) && <option value={definition.tts_engine_id} disabled>{engines.find((engine) => engine.id === definition.tts_engine_id)?.name ?? definition.tts_engine_id} · 当前不可用</option>}{ttsEngines.map((engine) => <option key={engine.id} value={engine.id}>{engine.name} · {engine.status}</option>)}</select></label><EngineQuickView api={api} engineId={definition.tts_engine_id} onUpdated={onEngineUpdated} /><label><span>默认音色</span><select value={ttsConfig.default_voice} onChange={(event) => onChange({ ...definition, tts: { ...ttsConfig, default_voice: event.target.value } })}><option value="">请选择</option>{voices.map((voice) => <option key={voice}>{voice}</option>)}</select></label><div className="agent-node-fields"><div className="tag-editor"><div>{voices.map((voice) => <button type="button" key={voice} onClick={() => removeVoice(voice)}>{voice}<span>×</span></button>)}</div></div><PresetVoicePicker api={api} engine={selectedTtsEngine} onAdd={(value) => { if (voices.includes(value)) return; onChange({ ...definition, tts: { ...ttsConfig, allowed_voices: [...voices, value], default_voice: ttsConfig.default_voice || value } }); }} /></div></div>}
            {selectedId === "backend" && <AgentBackendBindingFields api={api} backends={backends} definition={definition} contextPreview={agentBackendContextPreview} contextPreviewStale={agentBackendContextPreviewStale} onChange={onChange} />}
            {selectedId === "$input" && <p className="agent-boundary-copy">Realtime 会话提供用户语音输入，固定拓扑的输入端口不可修改。</p>}
            {selectedId === "$output" && <p className="agent-boundary-copy">通过 Realtime 会话输出合成语音，固定拓扑的输出端口不可修改。</p>}
            {selectedDiagnostics.map((item) => <div className="inline-error" key={item.code}>{item.message}</div>)}
          </Tabs.Content>
          <Tabs.Content value="ports" className="audio-inspector-body agent-inspector-ports">{(["inputs", "outputs"] as const).map(direction => <section key={direction}><h3>{direction === "inputs" ? "输入" : "输出"} · {selectedNode.data[direction].length}</h3><ul>{selectedNode.data[direction].map(port => {
            const links = edges.filter(edge => direction === "inputs" ? edge.target === selectedId && edge.targetHandle === port.id : edge.source === selectedId && edge.sourceHandle === port.id);
            return <li key={port.id}><AgentPort nodeId={selectedId} portId={port.id} /><div><span>{port.label}</span><small>{links.length ? links.map(edge => { const peer = nodes.find(node => node.id === (direction === "inputs" ? edge.source : edge.target)); return peer?.data.subtitle ?? "未知节点"; }).join("、") : "未连接"}</small></div></li>;
          })}</ul></section>)}<p className="agent-boundary-copy">固定拓扑，仅展示实际输入输出与连接关系。</p></Tabs.Content>
          </Tabs.Root>
        </aside>}
  </div>;
}
