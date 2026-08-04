import { useEffect } from "react";

import type {
  DuplexTtsConfig,
  EngineAsset,
  EngineKind,
  StageWorkflowDefinition,
  WorkflowKind,
} from "../api/types";

interface Props {
  definition: StageWorkflowDefinition;
  kind: WorkflowKind;
  engines: EngineAsset[];
  onChange: (definition: StageWorkflowDefinition) => void;
}

const labels: Record<string, string> = {
  physical_vad: "物理 VAD",
  semantic_vad: "语义 VAD",
  asr: "语音识别",
  responses_agent: "大模型 Responses",
  duplex_tts: "双向流式 TTS",
};

function clone(definition: StageWorkflowDefinition): StageWorkflowDefinition {
  return structuredClone(definition);
}

export function updateAllowedVoices(
  config: DuplexTtsConfig,
  input: string,
): DuplexTtsConfig {
  const allowed_voices = input
    .split(",")
    .map((voice) => voice.trim())
    .filter(Boolean);
  return {
    ...config,
    allowed_voices,
    default_voice: allowed_voices.includes(config.default_voice)
      ? config.default_voice
      : (allowed_voices[0] ?? ""),
  };
}

function EngineSelect({
  value,
  kind,
  engines,
  onChange,
}: {
  value: string | null;
  kind: EngineKind;
  engines: EngineAsset[];
  onChange: (value: string | null) => void;
}) {
  const compatible = engines.filter((engine) => engine.kind === kind && engine.enabled);
  return (
    <label>
      <span>执行引擎</span>
      <select value={value ?? ""} onChange={(event) => onChange(event.target.value || null)}>
        <option value="">请选择 {kind.toUpperCase()} 引擎</option>
        {compatible.map((engine) => (
          <option key={engine.id} value={engine.id}>
            {engine.name} · {engine.status === "healthy" ? "健康" : engine.status}
          </option>
        ))}
      </select>
      {compatible.length === 0 && <small className="field-warning">引擎库中没有可用的兼容引擎</small>}
    </label>
  );
}

function NumberField({ label, value, min, max, step = 0.1, onChange }: { label: string; value: number; min: number; max?: number; step?: number; onChange: (value: number) => void }) {
  return <label><span>{label}</span><input type="number" value={value} min={min} max={max} step={step} onChange={(event) => onChange(Number(event.target.value))} /></label>;
}

export function StageBuilder({ definition, kind, engines, onChange }: Props) {
  useEffect(() => {
    const tts = definition.stages.duplex_tts;
    if (!tts) return;
    const defaultVoice = tts.config.allowed_voices[0] ?? "";
    if (
      tts.config.allowed_voices.includes(tts.config.default_voice)
      || tts.config.default_voice === defaultVoice
    ) return;
    const next = clone(definition);
    if (next.stages.duplex_tts) {
      next.stages.duplex_tts.config.default_voice = defaultVoice;
    }
    onChange(next);
  }, [definition, onChange]);

  const updateEngine = (stageName: keyof StageWorkflowDefinition["stages"], engineId: string | null) => {
    const next = clone(definition);
    const stage = next.stages[stageName];
    if (stage) stage.engine_id = engineId;
    onChange(next);
  };

  const updatePhysical = (field: string, value: number) => {
    const next = clone(definition);
    next.stages.physical_vad.config = { ...next.stages.physical_vad.config, [field]: value };
    onChange(next);
  };

  const toggleSemantic = (enabled: boolean) => {
    const next = clone(definition);
    next.stages.semantic_vad = enabled
      ? { engine_id: null, config: {}, failure_policy: "fail_open" }
      : null;
    onChange(next);
  };

  const stages = [
    { key: "physical_vad", title: labels.physical_vad },
    ...(definition.stages.semantic_vad ? [{ key: "semantic_vad", title: labels.semantic_vad }] : []),
    { key: "asr", title: labels.asr },
    ...(kind === "voice_agent" ? [{ key: "responses_agent", title: labels.responses_agent }, { key: "duplex_tts", title: labels.duplex_tts }] : []),
  ];

  return (
    <div className="builder-layout">
      <section className="panel topology-panel">
        <div className="panel-heading"><div><h2>真实运行拓扑</h2><p>顺序由工作流类型固定，所有连线均由服务端编译器生成。</p></div><span className="status-chip">只读拓扑</span></div>
        <div className="stage-topology" aria-label="运行拓扑">
          <div className="topology-node boundary-node"><strong>音频输入</strong><small>24 kHz PCM</small></div>
          {stages.map((stage) => <div className="topology-segment" key={stage.key}><span className="topology-arrow">→</span><div className="topology-node"><strong>{stage.title}</strong><small>{definition.stages[stage.key as keyof typeof definition.stages]?.engine_id ? "已绑定" : "待绑定"}</small></div></div>)}
          <div className="topology-segment"><span className="topology-arrow">→</span><div className="topology-node boundary-node"><strong>会话输出</strong><small>转写 · 文本 · 音频</small></div></div>
        </div>
        <div className="event-rail"><span>事件汇聚</span><div /><small>VAD、转写、模型、TTS 状态均进入会话输出</small></div>
      </section>

      <section className="panel stage-panel">
        <div className="panel-heading"><div><h2>阶段配置</h2><p>修改会自动保存；发布前会重新校验并探测全部引擎。</p></div></div>

        <article className="stage-config-card" id="stage-physical_vad">
          <div className="stage-card-heading"><span className="stage-index">01</span><div><h3>物理 VAD</h3><p>检测语音边界并形成可识别音频片段。</p></div><span className="required-mark">必需</span></div>
          <div className="form-grid two-columns">
            <EngineSelect value={definition.stages.physical_vad.engine_id} kind="vad" engines={engines} onChange={(value) => updateEngine("physical_vad", value)} />
            <NumberField label="最短片段（秒）" value={definition.stages.physical_vad.config.min_segment_seconds} min={0} onChange={(value) => updatePhysical("min_segment_seconds", value)} />
            <NumberField label="最长片段（秒）" value={definition.stages.physical_vad.config.max_segment_seconds} min={0.1} onChange={(value) => updatePhysical("max_segment_seconds", value)} />
            <NumberField label="音频缓冲（秒）" value={definition.stages.physical_vad.config.audio_buffer_seconds} min={0.1} onChange={(value) => updatePhysical("audio_buffer_seconds", value)} />
            <NumberField label="语音前滚（秒）" value={definition.stages.physical_vad.config.speech_pre_roll_seconds} min={0} onChange={(value) => updatePhysical("speech_pre_roll_seconds", value)} />
          </div>
          {definition.stages.physical_vad.config.audio_buffer_seconds < definition.stages.physical_vad.config.max_segment_seconds + definition.stages.physical_vad.config.speech_pre_roll_seconds && <div className="inline-error">音频缓冲必须覆盖最长片段与语音前滚之和。</div>}
        </article>

        <article className="stage-config-card" id="stage-semantic_vad">
          <div className="stage-card-heading"><span className="stage-index">02</span><div><h3>语义 VAD</h3><p>可选的语义断句阶段；失败时默认跳过。</p></div><label className="switch-control"><input type="checkbox" checked={definition.stages.semantic_vad !== null} onChange={(event) => toggleSemantic(event.target.checked)} /><span>{definition.stages.semantic_vad ? "已启用" : "未启用"}</span></label></div>
          {definition.stages.semantic_vad && <div className="form-grid two-columns"><EngineSelect value={definition.stages.semantic_vad.engine_id} kind="vad" engines={engines} onChange={(value) => updateEngine("semantic_vad", value)} /><label><span>失败策略</span><select value={definition.stages.semantic_vad.failure_policy} onChange={(event) => { const next = clone(definition); if (next.stages.semantic_vad) next.stages.semantic_vad.failure_policy = event.target.value as "fail_open" | "fail_pipeline"; onChange(next); }}><option value="fail_open">失败时跳过</option><option value="fail_pipeline">失败时终止会话</option></select></label></div>}
        </article>

        <article className="stage-config-card" id="stage-asr">
          <div className="stage-card-heading"><span className="stage-index">03</span><div><h3>语音识别</h3><p>输出实时转写并触发后续 Agent。</p></div><span className="required-mark">必需</span></div>
          <EngineSelect value={definition.stages.asr.engine_id} kind="asr" engines={engines} onChange={(value) => updateEngine("asr", value)} />
        </article>

        {definition.stages.responses_agent && <article className="stage-config-card" id="stage-responses_agent">
          <div className="stage-card-heading"><span className="stage-index">04</span><div><h3>大模型 Responses</h3><p>接收最终转写，流式生成文本与工具调用。</p></div><span className="required-mark">必需</span></div>
          <div className="form-grid two-columns">
            <EngineSelect value={definition.stages.responses_agent.engine_id} kind="llm" engines={engines} onChange={(value) => updateEngine("responses_agent", value)} />
            <label><span>模型名称</span><input value={definition.stages.responses_agent.config.model} onChange={(event) => { const next = clone(definition); if (next.stages.responses_agent) next.stages.responses_agent.config.model = event.target.value; onChange(next); }} placeholder="输入服务实际支持的模型名" /></label>
            <label className="switch-control"><input type="checkbox" checked={definition.stages.responses_agent.config.thinking_enabled} onChange={(event) => { const next = clone(definition); if (next.stages.responses_agent) next.stages.responses_agent.config.thinking_enabled = event.target.checked; onChange(next); }} /><span>启用思考（默认关闭）</span></label>
            <label className="full-span"><span>系统指令</span><textarea rows={5} value={definition.stages.responses_agent.config.instructions} onChange={(event) => { const next = clone(definition); if (next.stages.responses_agent) next.stages.responses_agent.config.instructions = event.target.value; onChange(next); }} placeholder="定义 Agent 的角色、边界和回复方式" /></label>
            <NumberField label="最大输出 Token" value={definition.stages.responses_agent.config.max_output_tokens} min={1} max={32768} step={1} onChange={(value) => { const next = clone(definition); if (next.stages.responses_agent) next.stages.responses_agent.config.max_output_tokens = value; onChange(next); }} />
            <NumberField label="历史消息上限" value={definition.stages.responses_agent.config.history_max_items} min={8} max={4096} step={1} onChange={(value) => { const next = clone(definition); if (next.stages.responses_agent) next.stages.responses_agent.config.history_max_items = value; onChange(next); }} />
            <label className="full-span"><span>服务工具（逗号分隔）</span><input value={definition.stages.responses_agent.config.service_tools.join(", ")} onChange={(event) => { const next = clone(definition); if (next.stages.responses_agent) next.stages.responses_agent.config.service_tools = event.target.value.split(",").map((item) => item.trim()).filter(Boolean); onChange(next); }} placeholder="weather, search" /></label>
          </div>
          <div className="nested-config">
            <div className="nested-heading"><div><h4>MCP 服务</h4><p>每个服务独立配置地址、审批策略和允许工具。</p></div><button className="secondary-button" onClick={() => { const next = clone(definition); next.stages.responses_agent?.config.mcp_servers.push({ server_label: `mcp-${(next.stages.responses_agent?.config.mcp_servers.length ?? 0) + 1}`, server_url: "", require_approval: "always", allowed_tools: null, headers_env: {} }); onChange(next); }}>添加 MCP</button></div>
            {definition.stages.responses_agent.config.mcp_servers.length === 0 ? <div className="nested-empty">未配置 MCP 服务</div> : definition.stages.responses_agent.config.mcp_servers.map((server, index) => <div className="nested-row" key={`${server.server_label}-${index}`}>
              <label><span>标识</span><input value={server.server_label} onChange={(event) => { const next = clone(definition); if (next.stages.responses_agent) next.stages.responses_agent.config.mcp_servers[index].server_label = event.target.value; onChange(next); }} /></label>
              <label><span>HTTP(S) 地址</span><input value={server.server_url} onChange={(event) => { const next = clone(definition); if (next.stages.responses_agent) next.stages.responses_agent.config.mcp_servers[index].server_url = event.target.value; onChange(next); }} /></label>
              <label><span>调用审批</span><select value={server.require_approval} onChange={(event) => { const next = clone(definition); if (next.stages.responses_agent) next.stages.responses_agent.config.mcp_servers[index].require_approval = event.target.value as "always" | "never"; onChange(next); }}><option value="always">每次审批</option><option value="never">无需审批</option></select></label>
              <label><span>允许工具</span><input value={(server.allowed_tools ?? []).join(", ")} onChange={(event) => { const next = clone(definition); if (next.stages.responses_agent) { const tools = event.target.value.split(",").map((item) => item.trim()).filter(Boolean); next.stages.responses_agent.config.mcp_servers[index].allowed_tools = tools.length ? tools : null; } onChange(next); }} placeholder="留空表示全部" /></label>
              <button className="danger-text nested-remove" onClick={() => { const next = clone(definition); next.stages.responses_agent?.config.mcp_servers.splice(index, 1); onChange(next); }}>移除</button>
            </div>)}
          </div>
        </article>}

        {definition.stages.duplex_tts && <article className="stage-config-card" id="stage-duplex_tts">
          <div className="stage-card-heading"><span className="stage-index">05</span><div><h3>双向流式 TTS</h3><p>边接收模型文本，边合成并输出音频。</p></div><span className="required-mark">必需</span></div>
          <div className="form-grid two-columns">
            <EngineSelect value={definition.stages.duplex_tts.engine_id} kind="tts" engines={engines} onChange={(value) => updateEngine("duplex_tts", value)} />
            <label><span>允许音色（逗号分隔）</span><input value={definition.stages.duplex_tts.config.allowed_voices.join(", ")} onChange={(event) => { const next = clone(definition); if (next.stages.duplex_tts) next.stages.duplex_tts.config = updateAllowedVoices(next.stages.duplex_tts.config, event.target.value); onChange(next); }} /></label>
            <label><span>默认音色</span><select value={definition.stages.duplex_tts.config.default_voice} onChange={(event) => { const next = clone(definition); if (next.stages.duplex_tts) next.stages.duplex_tts.config.default_voice = event.target.value; onChange(next); }}>{definition.stages.duplex_tts.config.allowed_voices.map((voice) => <option key={voice} value={voice}>{voice}</option>)}</select></label>
            <label><span>语言</span><input value={definition.stages.duplex_tts.config.language} onChange={(event) => { const next = clone(definition); if (next.stages.duplex_tts) next.stages.duplex_tts.config.language = event.target.value; onChange(next); }} /></label>
          </div>
          {!definition.stages.duplex_tts.config.allowed_voices.includes(definition.stages.duplex_tts.config.default_voice) && <div className="inline-error">默认音色必须包含在允许音色中。</div>}
        </article>}
      </section>
    </div>
  );
}
