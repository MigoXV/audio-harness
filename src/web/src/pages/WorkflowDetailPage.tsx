import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router-dom";

import { ApiError, errorMessage } from "../api/client";
import type {
  AudioHarnessApi,
  AudioNodeType,
  AudioWorkflow,
  AudioWorkflowDocument,
  AudioWorkflowRevision,
  AudioWorkflowValidation,
  EngineAsset,
  VectorSpace,
} from "../api/types";
import { AudioGraphEditor } from "../components/AudioGraphEditor";
import { DeploymentPanel } from "../components/DeploymentPanel";
import { RealtimeTestPanel } from "../components/RealtimeTestPanel";
import { UnsavedChangesGuard } from "../components/UnsavedChangesGuard";
import { WorkbenchSidebar } from "../components/WorkbenchSidebar";

interface Props { api: AudioHarnessApi; refreshVersion: number; }
type SaveState = "idle" | "dirty" | "saving" | "saved" | "error" | "conflict";
const tabs = [["build", "构建"], ["access", "接入"], ["test", "测试"], ["history", "历史"]] as const;

export function WorkflowDetailPage({ api, refreshVersion }: Props) {
  const { workflowId = "", tab = "build" } = useParams();
  const navigate = useNavigate();
  const [workflow, setWorkflow] = useState<AudioWorkflow | null>(null);
  const [document, setDocument] = useState<AudioWorkflowDocument | null>(null);
  const [nodeTypes, setNodeTypes] = useState<AudioNodeType[]>([]);
  const [engines, setEngines] = useState<EngineAsset[]>([]);
  const updateEngine = useCallback((engine: EngineAsset) => {
    setEngines((items) => items.some((item) => item.id === engine.id) ? items.map((item) => item.id === engine.id ? engine : item) : [...items, engine]);
  }, []);
  const [vectorSpaces, setVectorSpaces] = useState<VectorSpace[]>([]);
  const [vectorWarning, setVectorWarning] = useState<string | null>(null);
  const [validation, setValidation] = useState<AudioWorkflowValidation>({ valid: false, diagnostics: [], topological_order: [] });
  const [revisions, setRevisions] = useState<AudioWorkflowRevision[]>([]);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [validating, setValidating] = useState(false);
  const [validationNotice, setValidationNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draftRevision, setDraftRevision] = useState(1);
  const [sidebarOpen, setSidebarOpen] = useState(() => typeof window.matchMedia !== "function" || !window.matchMedia("(max-width: 1100px)").matches);
  const changeVersion = useRef(0);
  const savedDocument = useRef<AudioWorkflowDocument | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [items, draft, types, engineList, history] = await Promise.all([
        api.listAudioWorkflows(), api.getAudioWorkflowDraft(workflowId), api.listAudioNodeTypes(),
        api.listEngines({ limit: 200 }), api.listAudioWorkflowRevisions(workflowId),
      ]);
      const summary = items.find((item) => item.id === workflowId);
      if (!summary) throw new Error("语音工作流不存在");
      setWorkflow(summary); setDocument(draft.document); savedDocument.current = structuredClone(draft.document); setNodeTypes(types); setEngines(engineList.items); setRevisions(history);
      setDraftRevision(draft.revision); setSaveState("idle");
      setValidation(await api.validateAudioWorkflow(workflowId));
    } catch (reason) { setError(errorMessage(reason, "工作流读取失败。")); }
    finally { setLoading(false); }
  }, [api, workflowId]);
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, refreshVersion]);
  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      setVectorSpaces([]); setVectorWarning(null);
      if (typeof api.listVectorSpaces !== "function") return;
      try {
        const infrastructure = await api.getVectorInfrastructure();
        if (cancelled) return;
        if (!infrastructure.configured) {
          setVectorWarning("尚未配置向量存储，向量检索相关功能暂不可用；仍可编辑和保存工作流。");
          return;
        }
        const spaces = await api.listVectorSpaces();
        if (!cancelled) setVectorSpaces(spaces);
      } catch (reason) {
        if (!cancelled) setVectorWarning(`向量空间读取失败：${errorMessage(reason, "向量存储不可用。")} 仍可编辑和保存工作流。`);
      }
    }, 0);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [api, workflowId, refreshVersion]);
  useEffect(() => {
    if (tab === "overview") navigate(`/audio-workflows/${encodeURIComponent(workflowId)}/build`, { replace: true });
    if (tab === "publish") navigate(`/audio-workflows/${encodeURIComponent(workflowId)}/access`, { replace: true });
  }, [navigate, tab, workflowId]);

  const saveDraft = useCallback(async (): Promise<boolean> => {
    if (!document) return false;
    const version = changeVersion.current;
    setSaveState("saving");
    try {
      const saved = await api.saveAudioWorkflowDraft(workflowId, document, draftRevision);
      savedDocument.current = structuredClone(saved.document);
      setDraftRevision(saved.revision);
      const unchanged = version === changeVersion.current;
      if (unchanged) setSaveState("saved"); else setSaveState("dirty");
      setValidation(await api.validateAudioWorkflow(workflowId));
      return unchanged;
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) setSaveState("conflict");
      else { setSaveState("error"); setError(errorMessage(reason, "草稿保存失败。")); }
      return false;
    }
  }, [api, document, draftRevision, workflowId]);

  useEffect(() => {
    const handleSaveShortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (saveState === "dirty" || saveState === "error") void saveDraft();
      }
    };
    window.addEventListener("keydown", handleSaveShortcut);
    return () => window.removeEventListener("keydown", handleSaveShortcut);
  }, [saveDraft, saveState]);

  const change = (next: AudioWorkflowDocument) => {
    changeVersion.current += 1; setDocument(next); setSaveState("dirty"); setValidationNotice(null);
  };
  const discardChanges = () => {
    if (savedDocument.current) setDocument(structuredClone(savedDocument.current));
    changeVersion.current += 1;
    setSaveState("saved");
    setError(null);
  };
  const publishBlocked = useMemo(() => {
    if (["dirty", "saving", "error", "conflict"].includes(saveState)) return "请先保存草稿";
    if (!validation.valid) return "先修复图校验错误";
    return null;
  }, [saveState, validation.valid]);
  const publish = async () => {
    if (publishBlocked) return;
    try { await api.publishAudioWorkflow(workflowId, draftRevision); await load(); }
    catch (reason) { setError(errorMessage(reason, "发布失败，当前活动版本未改变。")); }
  };
  const restore = async (value: number) => {
    try { await api.restoreAudioWorkflowRevision(workflowId, value, draftRevision); await load(); navigate(`/audio-workflows/${encodeURIComponent(workflowId)}/build`); }
    catch (reason) { setError(errorMessage(reason, "恢复历史版本失败。")); }
  };

  if (loading) return <main className="workspace-page"><div className="empty-state">正在加载工作流…</div></main>;
  if (!workflow || !document) return <main className="workspace-page"><div className="alert alert-error">{error ?? "工作流不存在"}</div></main>;
  const saveLabel = ({ idle: "草稿已同步", dirty: "有未保存修改", saving: "正在保存…", saved: "草稿已同步", error: "保存失败", conflict: "版本冲突" } as const)[saveState];
  const hasUnsavedChanges = ["dirty", "saving", "error", "conflict"].includes(saveState);
  const workflowNavigation = <nav className="builder-detail-navigation" aria-label="工作流页面">{tabs.map(([key, label]) => <NavLink key={key} to={`/audio-workflows/${encodeURIComponent(workflowId)}/${key}`} className={tab === key ? "active" : ""}>{label}<span>›</span></NavLink>)}</nav>;
  const validateDraft = async () => {
    setValidating(true); setValidationNotice(null);
    try {
      const result = await api.validateAudioWorkflow(workflowId);
      setValidation(result);
      setValidationNotice(result.valid ? `已保存的草稿 r${draftRevision} 校验通过` : `校验未通过：${result.diagnostics.map((item) => item.message).join("；")}`);
    }
    catch (reason) { setError(errorMessage(reason, "工作流校验失败。")); }
    finally { setValidating(false); }
  };
  const sidebarContent = <>
    <section className="builder-workflow-summary">
      <Link className="builder-back-link" to="/audio-workflows">← 工作流列表</Link>
      <div className="builder-workflow-title"><h1>{workflow.name}</h1><span className={`status-chip ${workflow.active_revision ? "status-published_healthy" : "status-unpublished"}`}>{workflow.active_revision ? `活动 v${workflow.active_revision}` : "未发布"}</span></div>
      <p><code>{workflow.slug}</code><span><small>草稿 r{draftRevision}</small> · {document.nodes.length} 个节点</span></p>
    </section>
    {tab !== "build" && workflowNavigation}
    {error && <div className="builder-sidebar-alert alert alert-error">{error}<button onClick={() => setError(null)}>关闭</button></div>}
    {vectorWarning && <div className="builder-sidebar-alert alert alert-warning" role="status">{vectorWarning}</div>}
    {saveState === "conflict" && <div className="builder-sidebar-alert alert alert-warning"><strong>服务器草稿已更新</strong><button className="secondary-button" onClick={() => void load()}>重新载入</button></div>}
  </>;
  const sidebarFooter = tab === "build" ? <div className="workflow-save-bar">
    <span className={`workflow-save-status save-${saveState}`} role="status"><i aria-hidden="true" />{saveState === "idle" || saveState === "saved" ? "已保存" : saveLabel}</span>
    {(["dirty", "error", "saving"].includes(saveState)) && <button className="text-button" aria-label="保存草稿" disabled={saveState === "saving"} onClick={() => void saveDraft()}>{saveState === "saving" ? "保存中" : "保存"}</button>}
  </div> : <div className="workflow-sidebar-footer">
    <div className={`builder-save-state save-${saveState}`}><i /><div><strong>{saveLabel}</strong><small>草稿 r{draftRevision}</small></div></div>
    <button className="secondary-button" disabled={saveState !== "dirty" && saveState !== "error"} onClick={() => void saveDraft()}>{saveState === "saving" ? "正在保存…" : "保存草稿"}</button>
  </div>;
  const publishReview = <section className="panel publish-review"><div className="panel-heading"><div><p className="eyebrow">ACTIVE VERSION</p><h2>发布当前草稿</h2><p>编译并发布后，即可使用资源 Slug 接入 Realtime 或开始测试；发布失败不会替换当前活动版本。</p></div><span className={`status-chip ${workflow.active_revision ? "status-published_healthy" : "status-unpublished"}`}>{workflow.active_revision ? `当前活动 v${workflow.active_revision}` : "尚无活动版本"}</span></div><div className="draft-version-line">草稿修订 r{draftRevision}</div>{validation.diagnostics.length ? <div className="diagnostic-list">{validation.diagnostics.map((item, index) => <div key={`${item.code}-${index}`}><strong>{item.message}</strong><small>{item.code}</small></div>)}</div> : <div className="success-block">结构校验通过，共 {document.nodes.length} 个节点、{document.edges.length} 条内部连线。</div>}<div className="panel-actions"><button className="primary-button large-button" disabled={Boolean(publishBlocked)} onClick={() => void publish()}>编译并发布</button>{publishBlocked && <span className="blocking-reason">{publishBlocked}</span>}</div></section>;

  return <main className={`workspace-page workflow-detail-page workflow-sidebar-page ${tab === "build" ? "workflow-building" : ""}`}>
    <UnsavedChangesGuard when={hasUnsavedChanges} saving={saveState === "saving"} onSave={saveDraft} onDiscard={discardChanges} />
    {tab === "build" ? <AudioGraphEditor
      key={workflow.id}
      api={api}
      onEngineUpdated={updateEngine}
      document={document}
      nodeTypes={nodeTypes}
      engines={engines}
      vectorSpaces={vectorSpaces}
      diagnostics={validation.diagnostics}
      sidebarOpen={sidebarOpen}
      sidebarContent={sidebarContent}
      sidebarFooter={sidebarFooter}
      toolbarContent={workflowNavigation}
      testHref={`/audio-workflows/${encodeURIComponent(workflowId)}/test`}
      onValidate={validateDraft}
      validating={validating}
      validationNotice={validationNotice}
      validationBlockedReason={hasUnsavedChanges ? "请先保存当前草稿，再校验已保存的版本" : undefined}
      onSidebarOpenChange={setSidebarOpen}
      onChange={change}
    /> : <div className={`workflow-detail-layout ${sidebarOpen ? "palette-open" : "palette-closed"}`}>
      <WorkbenchSidebar open={sidebarOpen} expandedTitle="工作流工具" collapsedTitle="工作流" ariaLabel="工作流侧栏" content={sidebarContent} footer={sidebarFooter} onOpenChange={setSidebarOpen} />
      <section className={`workflow-detail-content ${tab === "history" ? "object-history-content" : ""}`}>
        {tab === "access" && <div className="workflow-access-content">{publishReview}<DeploymentPanel targetType="audio_workflow" slug={workflow.slug} activeRevision={workflow.active_revision} /></div>}
        {tab === "test" && <div className="workflow-test-content">
          {workflow.active_revision === null ? <section className="panel test-prerequisite"><p className="eyebrow">REALTIME TEST</p><h2>发布后开始测试</h2><p>请先在接入页编译并发布活动版本，发布后即可使用资源 Slug 开始测试。</p><button className="secondary-button" onClick={() => navigate(`/audio-workflows/${encodeURIComponent(workflowId)}/access`)}>前往接入配置</button></section> : <RealtimeTestPanel api={api} model={workflow.slug} workflowKind="transcription" canTest blockedReason="" />}
        </div>}
        {tab === "history" && <section className="panel object-history-panel"><div className="panel-heading"><div><h2>不可变发布历史</h2><p>恢复只会创建新草稿，不会热改活动会话。</p></div></div><div className="object-records" tabIndex={0} role="region" aria-label="发布记录">{revisions.length === 0 ? <div className="empty-state">尚无发布记录</div> : <div className="revision-list">{revisions.map((item) => <div className="revision-row" key={item.revision}><div><strong>v{item.revision}</strong><small>{item.document.nodes.length} 个节点</small></div><span>{Object.keys(item.engine_revisions).length} 个引擎快照</span><button className="secondary-button" onClick={() => void restore(item.revision)}>恢复为草稿</button></div>)}</div>}</div></section>}
      </section>
    </div>}
  </main>;
}
