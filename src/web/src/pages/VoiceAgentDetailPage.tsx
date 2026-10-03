import { useCallback, useEffect, useRef, useState } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router-dom";

import { errorMessage } from "../api/client";
import type {
  AgentBackend,
  AudioHarnessApi,
  AudioWorkflow,
  EngineAsset,
  VoiceAgent,
  VoiceAgentDefinition,
  VoiceAgentDraft,
  VoiceAgentRevision,
  VoiceAgentValidation,
} from "../api/types";
import { DeploymentPanel } from "../components/DeploymentPanel";
import { RealtimeTestPanel } from "../components/RealtimeTestPanel";
import { UnsavedChangesGuard } from "../components/UnsavedChangesGuard";
import { VoiceAgentGraphEditor } from "../components/VoiceAgentGraphEditor";
import { WorkbenchSidebar } from "../components/WorkbenchSidebar";

interface Props { api: AudioHarnessApi; refreshVersion: number; }
const tabs = [["build", "构建"], ["access", "接入"], ["test", "测试"], ["history", "历史"]] as const;

export function VoiceAgentDetailPage({ api, refreshVersion }: Props) {
  const { profileId = "", tab = "build" } = useParams();
  const navigate = useNavigate();
  const [summary, setSummary] = useState<VoiceAgent | null>(null);
  const [draft, setDraft] = useState<VoiceAgentDraft | null>(null);
  const [definition, setDefinition] = useState<VoiceAgentDefinition | null>(null);
  const [workflows, setWorkflows] = useState<AudioWorkflow[]>([]);
  const [engines, setEngines] = useState<EngineAsset[]>([]);
  const updateEngine = useCallback((engine: EngineAsset) => {
    setEngines((items) => items.some((item) => item.id === engine.id) ? items.map((item) => item.id === engine.id ? engine : item) : [...items, engine]);
  }, []);
  const [backends, setBackends] = useState<AgentBackend[]>([]);
  const [history, setHistory] = useState<VoiceAgentRevision[]>([]);
  const [validation, setValidation] = useState<VoiceAgentValidation | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(() => typeof window.matchMedia !== "function" || !window.matchMedia("(max-width: 1100px)").matches);
  const savedDefinition = useRef<VoiceAgentDefinition | null>(null);

  const load = useCallback(async () => {
    try {
      const [agents, nextDraft, nextWorkflows, engineList, backendList, revisions] = await Promise.all([
        api.listVoiceAgents(), api.getVoiceAgentDraft(profileId), api.listAudioWorkflows(),
        api.listEngines({ limit: 200 }), api.listAgentBackends ? api.listAgentBackends() : Promise.resolve([]), api.listVoiceAgentRevisions(profileId),
      ]);
      const found = agents.find((item) => item.id === profileId);
      if (!found) throw new Error("语音智能体不存在");
      setSummary(found); setDraft(nextDraft); setDefinition(nextDraft.definition); savedDefinition.current = structuredClone(nextDraft.definition); setWorkflows(nextWorkflows);
      setEngines(engineList.items); setBackends(backendList); setHistory(revisions); setDirty(false);
      setValidation(await api.validateVoiceAgent(profileId)); setError(null);
    } catch (reason) { setError(errorMessage(reason, "语音智能体读取失败。")); }
  }, [api, profileId]);
  useEffect(() => { const timer = window.setTimeout(() => void load(), 0); return () => window.clearTimeout(timer); }, [load, refreshVersion]);
  useEffect(() => {
    if (tab === "overview" || tab === "configure") navigate(`/voice-agents/${encodeURIComponent(profileId)}/build`, { replace: true });
    if (tab === "publish") navigate(`/voice-agents/${encodeURIComponent(profileId)}/access`, { replace: true });
  }, [navigate, profileId, tab]);

  const change = (next: VoiceAgentDefinition) => { setDefinition(next); setDirty(true); };
  const save = async () => {
    if (!draft || !definition) return null;
    if (!dirty) return draft;
    const saved = await api.saveVoiceAgentDraft(profileId, definition, draft.revision);
    savedDefinition.current = structuredClone(saved.definition);
    setDraft(saved); setDefinition(saved.definition); setDirty(false);
    setValidation(await api.validateVoiceAgent(profileId));
    return saved;
  };
  const saveOnly = async (): Promise<boolean> => {
    setBusy(true); setError(null);
    try { await save(); return true; }
    catch (reason) { setError(errorMessage(reason, "语音智能体草稿保存失败。")); return false; }
    finally { setBusy(false); }
  };
  useEffect(() => {
    const handleSaveShortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (dirty && !busy) void saveOnly();
      }
    };
    window.addEventListener("keydown", handleSaveShortcut);
    return () => window.removeEventListener("keydown", handleSaveShortcut);
  });
  const publish = async () => {
    if (!draft || !definition || dirty) return;
    setBusy(true); setError(null);
    try {
      const checked = await api.validateVoiceAgent(profileId);
      setValidation(checked);
      if (!checked.valid) throw new Error(checked.diagnostics.map((item) => item.message).join("；"));
      await api.publishVoiceAgent(profileId, draft.revision);
      await load();
    } catch (reason) { setError(errorMessage(reason, "语音智能体发布失败，当前活动版本未改变。")); }
    finally { setBusy(false); }
  };
  const restore = async (revision: number) => {
    if (!draft) return;
    try { await api.restoreVoiceAgentRevision(profileId, revision, draft.revision); await load(); navigate(`/voice-agents/${profileId}/build`); }
    catch (reason) { setError(errorMessage(reason, "历史版本恢复失败。")); }
  };
  const discardChanges = () => {
    if (savedDefinition.current) setDefinition(structuredClone(savedDefinition.current));
    setDirty(false);
    setError(null);
  };

  if (!summary || !draft || !definition) return <main className="workspace-page"><div className={error ? "alert alert-error" : "empty-state"}>{error ?? "正在读取语音智能体…"}</div></main>;
  const saveLabel = busy ? "正在处理…" : dirty ? "有未保存修改" : "草稿已同步";
  const sidebarContent = <>
    <section className="builder-workflow-summary">
      <Link className="builder-back-link" to="/voice-agents">← 语音智能体列表</Link>
      <div className="builder-workflow-title"><h1>{summary.name}</h1><span className={`status-chip ${summary.active_revision ? "status-published_healthy" : "status-unpublished"}`}>{summary.active_revision ? `活动 v${summary.active_revision}` : "未发布"}</span></div>
      <p><code>{summary.slug}</code><span>草稿修订 r{draft.revision}</span></p>
    </section>
    <nav className="builder-detail-navigation" aria-label="语音智能体页面">{tabs.map(([key, label]) => <NavLink key={key} to={`/voice-agents/${encodeURIComponent(profileId)}/${key}`} className={tab === key ? "active" : ""}>{label}<span>›</span></NavLink>)}</nav>
    {error && <div className="builder-sidebar-alert alert alert-error">{error}<button onClick={() => setError(null)}>关闭</button></div>}
  </>;
  const sidebarFooter = <div className="agent-sidebar-footer">
    <div className={`builder-save-state ${busy ? "save-saving" : dirty ? "save-dirty" : "save-saved"}`}><i /><div><strong>{saveLabel}</strong><small>草稿修订 r{draft.revision}</small></div></div>
    <button className="secondary-button" disabled={busy || !dirty} onClick={() => void saveOnly()}>保存草稿</button>
  </div>;
  const publishReview = <section className="panel publish-review"><div className="panel-heading"><div><p className="eyebrow">ACTIVE VERSION</p><h2>发布当前草稿</h2><p>先明确保存草稿，再准备 Responses、TTS 与可选 Agent Backend 并发布不可变快照。</p></div><span className={`status-chip ${summary.active_revision ? "status-published_healthy" : "status-unpublished"}`}>{summary.active_revision ? `当前活动 v${summary.active_revision}` : "尚无活动版本"}</span></div><div className="draft-version-line">草稿修订 r{draft.revision}{dirty ? " · 包含未保存修改" : ""}</div>{validation?.diagnostics.length ? <div className="diagnostic-list">{validation.diagnostics.map((item) => <div key={item.code}><strong>{item.message}</strong><small>{item.code}</small></div>)}</div> : <div className="success-block">静态配置完整，可以准备资源并发布。</div>}{validation?.agent_backend_context_preview && <section className="agent-backend-context-preview"><h3>将冻结的 Agent Backend 系统上下文</h3><p>发布后，此安全摘要会随活动版本固定；运行时只补充会话状态。</p><pre>{validation.agent_backend_context_preview}</pre></section>}{validation?.speaker_context_preview && <section className="speaker-context-preview"><h3>将冻结的声纹提示词</h3><p>只在说话人匹配成功的当前回合补充可信身份；此提示词随发布版本固定。</p><pre>{validation.speaker_context_preview}</pre></section>}<div className="panel-actions"><button className="primary-button large-button" disabled={busy || dirty} onClick={() => void publish()}>{busy ? "正在发布…" : "编译并发布"}</button>{dirty && <span className="blocking-reason">请先保存草稿</span>}</div></section>;

  return <main className="workspace-page workflow-detail-page workflow-sidebar-page">
    <UnsavedChangesGuard when={dirty} saving={busy} onSave={saveOnly} onDiscard={discardChanges} />
    {tab === "build" ? <VoiceAgentGraphEditor
      key={definition.audio_workflow_id || "empty-workflow"}
      api={api}
      onEngineUpdated={updateEngine}
      definition={definition}
      workflows={workflows}
      engines={engines}
      backends={backends}
      diagnostics={validation?.diagnostics ?? []}
      agentBackendContextPreview={validation?.agent_backend_context_preview}
      agentBackendContextPreviewStale={dirty}
      speakerContextPreview={validation?.speaker_context_preview}
      speakerContextPreviewStale={dirty}
      sidebarOpen={sidebarOpen}
      sidebarContent={sidebarContent}
      sidebarFooter={sidebarFooter}
      onSidebarOpenChange={setSidebarOpen}
      onChange={change}
    /> : <div className={`workflow-detail-layout ${sidebarOpen ? "palette-open" : "palette-closed"}`}>
      <WorkbenchSidebar open={sidebarOpen} expandedTitle="智能体工具" collapsedTitle="智能体" ariaLabel="语音智能体侧栏" content={sidebarContent} footer={sidebarFooter} onOpenChange={setSidebarOpen} />
      <section className="workflow-detail-content">
        {tab === "access" && <div className="workflow-access-content">{publishReview}<DeploymentPanel targetType="voice_agent" slug={summary.slug} activeRevision={summary.active_revision} /></div>}
        {tab === "test" && <div className="workflow-test-content">
          {summary.active_revision === null ? <section className="panel test-prerequisite"><p className="eyebrow">REALTIME TEST</p><h2>发布后开始测试</h2><p>请先在接入页保存并发布活动版本，发布后即可使用资源 Slug 开始测试。</p><button className="secondary-button" onClick={() => navigate(`/voice-agents/${encodeURIComponent(profileId)}/access`)}>前往接入配置</button></section> : <RealtimeTestPanel api={api} model={summary.slug} workflowKind="voice_agent" canTest blockedReason="" />}
        </div>}
        {tab === "history" && <section className="panel"><div className="panel-heading"><div><h2>不可变发布历史</h2><p>恢复只创建新草稿，不改变活动会话。</p></div></div>{history.length === 0 ? <div className="empty-state">尚无发布记录</div> : <div className="revision-list">{history.map((item) => <div className="revision-row" key={item.revision}><div><strong>v{item.revision}</strong><small>语音工作流 v{item.audio_workflow_revision}</small></div><span>Responses / TTS{item.agent_backend_revision ? ` / Backend r${item.agent_backend_revision}` : ""} 已钉住</span><button className="secondary-button" onClick={() => void restore(item.revision)}>恢复为草稿</button></div>)}</div>}</section>}
      </section>
    </div>}
  </main>;
}
