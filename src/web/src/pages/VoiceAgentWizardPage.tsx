import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, errorMessage } from "../api/client";
import type { AudioHarnessApi, AudioWorkflow, EngineAsset, VoiceAgent, VoiceAgentDefinition } from "../api/types";
import { EngineModelPicker } from "../components/EngineModelPicker";
import { WorkbenchSidebar } from "../components/WorkbenchSidebar";
import { defaultVoiceAgentDefinition, normalizeSlug } from "../features/voiceAgents/form";

interface Props { api: AudioHarnessApi; }
const modelPattern = /^[a-z0-9][a-z0-9._-]*$/;

export function VoiceAgentWizardPage({ api }: Props) {
  const navigate = useNavigate();
  const [step, setStep] = useState(1);
  const [maxStep, setMaxStep] = useState(1);
  const [sidebarOpen, setSidebarOpen] = useState(() => typeof window.matchMedia !== "function" || !window.matchMedia("(max-width: 1100px)").matches);
  const [workflows, setWorkflows] = useState<AudioWorkflow[]>([]);
  const [engines, setEngines] = useState<EngineAsset[]>([]);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [definition, setDefinition] = useState<VoiceAgentDefinition>(defaultVoiceAgentDefinition());
  const [voiceInput, setVoiceInput] = useState("");
  const [routeEnabled, setRouteEnabled] = useState(true);
  const [routeModel, setRouteModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<VoiceAgent | null>(null);
  const [routeConflict, setRouteConflict] = useState(false);

  useEffect(() => {
    const load = async () => {
      try {
        const [nextWorkflows, engineList] = await Promise.all([api.listAudioWorkflows(), api.listEngines({ limit: 200 })]);
        const active = nextWorkflows.filter((item) => item.active_revision !== null);
        setWorkflows(active); setEngines(engineList.items);
        if (active[0]) setDefinition((current) => ({ ...current, audio_workflow_id: active[0].id }));
      } catch (reason) { setError(errorMessage(reason, "创建向导加载失败。")); }
    };
    void load();
  }, [api]);

  const normalizedSlug = normalizeSlug(slug);
  const effectiveRouteModel = routeModel || normalizedSlug;
  const routeModelValid = modelPattern.test(effectiveRouteModel);
  const responsesEngines = engines.filter((item) => item.enabled && item.api === "responses");
  const ttsEngines = engines.filter((item) => item.enabled && item.api === "qwen_tts_duplex");
  const selectedResponsesEngine = responsesEngines.find((item) => item.id === definition.responses_engine_id);
  const voices = definition.tts?.allowed_voices ?? [];
  const canContinue = useMemo(() => {
    if (step === 1) return Boolean(name.trim() && normalizedSlug);
    if (step === 2) return Boolean(definition.audio_workflow_id);
    if (step === 3) return Boolean(definition.responses_engine_id && definition.responses?.model?.trim() && definition.tts_engine_id && voices.length && definition.tts?.default_voice);
    return !routeEnabled || routeModelValid;
  }, [definition, name, normalizedSlug, routeEnabled, routeModelValid, step, voices.length]);

  const addVoice = () => {
    const value = voiceInput.trim();
    if (!value || voices.includes(value)) return;
    const next = [...voices, value];
    setDefinition({ ...definition, tts: { ...definition.tts!, allowed_voices: next, default_voice: definition.tts?.default_voice || value } });
    setVoiceInput("");
  };
  const removeVoice = (value: string) => {
    const next = voices.filter((item) => item !== value);
    setDefinition({ ...definition, tts: { ...definition.tts!, allowed_voices: next, default_voice: next.includes(definition.tts?.default_voice ?? "") ? definition.tts!.default_voice : next[0] ?? "" } });
  };

  const finish = async () => {
    setBusy(true); setError(null);
    let profile = created;
    try {
      if (!profile) {
        profile = await api.createVoiceAgent({ slug: normalizedSlug, name: name.trim(), description: null, definition });
        setCreated(profile);
      }
      const validation = await api.validateVoiceAgent(profile.id);
      if (!validation.valid) throw new Error(validation.diagnostics.map((item) => item.message).join("；"));
      await api.publishVoiceAgent(profile.id, profile.draft_revision);
      if (routeEnabled) {
        try {
          await api.putRealtimeRoute({ model: effectiveRouteModel, target_type: "voice_agent", target_id: profile.id, visibility: "public", replace_existing: false });
        } catch (reason) {
          if (reason instanceof ApiError && reason.code === "realtime_route_conflict") { setRouteConflict(true); return; }
          throw reason;
        }
      }
      navigate(`/voice-agents/${encodeURIComponent(profile.id)}/${routeEnabled ? "test" : "access"}`);
    } catch (reason) { setError(errorMessage(reason, "语音智能体创建未完成。")); }
    finally { setBusy(false); }
  };
  const replaceRoute = async () => {
    if (!created) return;
    setBusy(true);
    try {
      await api.putRealtimeRoute({ model: effectiveRouteModel, target_type: "voice_agent", target_id: created.id, visibility: "public", replace_existing: true });
      navigate(`/voice-agents/${encodeURIComponent(created.id)}/test`);
    } catch (reason) { setError(errorMessage(reason, "路由替换失败。")); }
    finally { setBusy(false); }
  };

  const stepLabels = ["基本信息", "语音工作流", "模型与声音", "确认接入"];
  const sidebarContent = <>
    <section className="builder-workflow-summary wizard-sidebar-summary">
      <button className="builder-back-link" onClick={() => navigate("/voice-agents")}>← 语音智能体列表</button>
      <div className="builder-workflow-title"><h1>新建语音智能体</h1></div>
      <p><span>四步完成配置、发布与公开接入</span></p>
    </section>
    <ol className="wizard-sidebar-steps" aria-label="创建步骤">{stepLabels.map((label, index) => {
      const number = index + 1;
      return <li className={step === number ? "active" : maxStep > number ? "done" : ""} key={label}><button disabled={number > maxStep || busy} onClick={() => setStep(number)}><span>{maxStep > number ? "✓" : number}</span><strong>{label}</strong></button></li>;
    })}</ol>
    {error && <div className="builder-sidebar-alert alert alert-error"><span>{error}</span>{created && <button className="secondary-button" onClick={() => navigate(`/voice-agents/${created.id}/configure`)}>打开已创建草稿</button>}</div>}
  </>;

  return <main className="workspace-page workflow-sidebar-page wizard-workbench-page">
    <div className={`workflow-detail-layout ${sidebarOpen ? "palette-open" : "palette-closed"}`}>
      <WorkbenchSidebar open={sidebarOpen} expandedTitle="创建语音智能体" collapsedTitle="创建" ariaLabel="语音智能体创建侧栏" content={sidebarContent} footer={<div className="wizard-step-count"><strong>第 {step} 步</strong><span>/ 4</span></div>} onOpenChange={setSidebarOpen} />
      <section className="workflow-detail-content wizard-detail-content">
        <section className="panel wizard-card">
          {step === 1 && <><div className="panel-heading"><div><p className="eyebrow">BASIC PROFILE</p><h2>给语音智能体一个清晰身份</h2><p>名称用于控制台识别；slug 是默认建议的 Realtime model。</p></div></div><div className="form-grid two-columns"><label><span>名称</span><input autoFocus value={name} onChange={(event) => { const value = event.target.value; setName(value); if (!slugEdited) setSlug(normalizeSlug(value)); }} placeholder="客服助手" /></label><label><span>Slug</span><input value={slug} onChange={(event) => { setSlugEdited(true); setSlug(normalizeSlug(event.target.value)); }} placeholder="support-agent" /><small>{normalizedSlug || (name.trim() ? "名称无法生成英文 Slug，请手动填写" : "小写字母、数字和连字符")}</small></label></div></>}
          {step === 2 && <><div className="panel-heading"><div><p className="eyebrow">AUDIO PIPELINE</p><h2>选择语音处理基础</h2><p>语音智能体只引用已发布工作流；工作流后续发布不会热改当前智能体版本。</p></div></div><div className="selection-cards">{workflows.map((workflow) => <button className={definition.audio_workflow_id === workflow.id ? "selected" : ""} key={workflow.id} onClick={() => setDefinition({ ...definition, audio_workflow_id: workflow.id })}><span className="radio-mark" /><div><strong>{workflow.name}</strong><small>{workflow.slug} · 活动 v{workflow.active_revision}</small></div></button>)}</div>{workflows.length === 0 && <div className="alert alert-warning">没有已发布语音工作流，请先完成工作流发布。</div>}</>}
          {step === 3 && <><div className="panel-heading"><div><p className="eyebrow">MODEL & VOICE</p><h2>配置回复与声音</h2><p>只显示已启用且 API 能力匹配的引擎，健康状态仍会在发布时重新确认。</p></div></div><div className="form-grid two-columns"><label><span>Responses 引擎</span><select value={definition.responses_engine_id ?? ""} onChange={(event) => setDefinition({ ...definition, responses_engine_id: event.target.value || null })}><option value="">请选择</option>{responsesEngines.map((engine) => <option key={engine.id} value={engine.id}>{engine.name} · {engine.status}</option>)}</select></label><EngineModelPicker api={api} engine={selectedResponsesEngine} value={definition.responses?.model ?? ""} onChange={(model) => setDefinition((current) => ({ ...current, responses: { ...current.responses!, model } }))} /><label className="full-span"><span>系统指令</span><textarea value={definition.responses?.instructions ?? ""} onChange={(event) => setDefinition({ ...definition, responses: { ...definition.responses!, instructions: event.target.value } })} placeholder="定义语音智能体的角色、边界和回复风格。" /></label><label><span>TTS 引擎</span><select value={definition.tts_engine_id ?? ""} onChange={(event) => setDefinition({ ...definition, tts_engine_id: event.target.value || null })}><option value="">请选择</option>{ttsEngines.map((engine) => <option key={engine.id} value={engine.id}>{engine.name} · {engine.status}</option>)}</select></label><label><span>默认音色</span><select value={definition.tts?.default_voice ?? ""} onChange={(event) => setDefinition({ ...definition, tts: { ...definition.tts!, default_voice: event.target.value } })}><option value="">先添加允许音色</option>{voices.map((voice) => <option key={voice} value={voice}>{voice}</option>)}</select></label><label className="full-span"><span>允许音色</span><div className="tag-editor"><div>{voices.map((voice) => <button key={voice} onClick={() => removeVoice(voice)}>{voice}<span>×</span></button>)}</div><input value={voiceInput} onChange={(event) => setVoiceInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addVoice(); } }} placeholder="输入音色名称后按 Enter" /><button className="secondary-button" onClick={addVoice}>添加</button></div></label></div></>}
          {step === 4 && <><div className="panel-heading"><div><p className="eyebrow">REVIEW</p><h2>确认发布与接入</h2><p>系统会先创建草稿、执行静态校验和引擎准备，再发布不可变版本。</p></div></div><div className="review-grid"><article><span>语音智能体</span><strong>{name}</strong><small>{normalizedSlug}</small></article><article><span>语音工作流</span><strong>{workflows.find((item) => item.id === definition.audio_workflow_id)?.name}</strong></article><article><span>Responses</span><strong>{engines.find((item) => item.id === definition.responses_engine_id)?.name}</strong><small>{definition.responses?.model}</small></article><article><span>TTS</span><strong>{engines.find((item) => item.id === definition.tts_engine_id)?.name}</strong><small>{definition.tts?.default_voice}</small></article></div><label className="switch-control route-switch"><input type="checkbox" checked={routeEnabled} onChange={(event) => setRouteEnabled(event.target.checked)} /><span>发布后创建公开 Realtime 路由</span></label>{routeEnabled && <label><span>Realtime model</span><input value={effectiveRouteModel} onChange={(event) => setRouteModel(event.target.value.toLowerCase())} /><small>{routeModelValid ? "将作为 WSS 与 WebRTC 的稳定入口" : "仅支持小写字母、数字、点、下划线或连字符"}</small></label>}{routeConflict && <div className="alert alert-warning"><div><strong>Realtime model 已被其他资源占用</strong><p>语音智能体已成功发布。你可以明确替换旧目标，或保留当前智能体为未接入状态。</p></div><div className="inline-actions"><button className="secondary-button" onClick={() => created && navigate(`/voice-agents/${created.id}/access`)}>暂不接入</button><button className="danger-button" disabled={busy} onClick={() => void replaceRoute()}>替换并继续</button></div></div>}</>}
        </section>
        <footer className="wizard-actions"><button className="secondary-button" disabled={busy || step === 1} onClick={() => setStep((value) => Math.max(1, value - 1))}>上一步</button>{step < 4 ? <button className="primary-button" disabled={!canContinue} onClick={() => { const next = step + 1; setStep(next); setMaxStep((value) => Math.max(value, next)); }}>下一步</button> : <button className="primary-button" disabled={busy || routeConflict || !canContinue} onClick={() => void finish()}>{busy ? "正在创建与发布…" : routeEnabled ? "创建、发布并接入" : "创建并发布"}</button>}</footer>
      </section>
    </div>
  </main>;
}
