import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router-dom";
import * as AlertDialog from "@radix-ui/react-alert-dialog";

import { ApiError, errorMessage } from "../api/client";
import type {
  AudioHarnessApi,
  EngineAsset,
  ManagedWorkflowDraft,
  PipelineRevision,
  StageWorkflowDefinition,
  WorkflowStatus,
  WorkflowSummary,
} from "../api/types";
import { RealtimeTestPanel } from "../components/RealtimeTestPanel";
import { StageBuilder } from "../components/StageBuilder";

interface Props { api: AudioHarnessApi; refreshVersion: number; }
type SaveState = "idle" | "dirty" | "saving" | "saved" | "error" | "conflict";

const tabs = [
  ["overview", "概览"], ["build", "构建"], ["test", "测试"], ["publish", "发布"], ["history", "历史"],
] as const;

const stateText: Record<string, string> = {
  incomplete: "配置未完成", unpublished: "待首次发布", unpublished_changes: "有未发布修改",
  published_healthy: "运行健康", published_degraded: "运行异常",
};

const healthText: Record<string, string> = {
  healthy: "健康", unhealthy: "不可用", unknown: "未探测", checking: "探测中", stale: "结果过期", disabled: "已停用", missing: "未绑定",
};

export function WorkflowDetailPage({ api, refreshVersion }: Props) {
  const { workflowId = "", tab = "overview" } = useParams();
  const navigate = useNavigate();
  const [workflow, setWorkflow] = useState<WorkflowSummary | null>(null);
  const [draft, setDraft] = useState<ManagedWorkflowDraft | null>(null);
  const [definition, setDefinition] = useState<StageWorkflowDefinition | null>(null);
  const [status, setStatus] = useState<WorkflowStatus | null>(null);
  const [engines, setEngines] = useState<EngineAsset[]>([]);
  const [revisions, setRevisions] = useState<PipelineRevision[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [publishing, setPublishing] = useState(false);
  const [publishConfirmOpen, setPublishConfirmOpen] = useState(false);
  const revisionRef = useRef(1);
  const changeVersion = useRef(0);

  const load = useCallback(async () => {
    if (!workflowId || !api.listWorkflows || !api.getManagedWorkflowDraft || !api.getWorkflowStatus) return;
    setLoading(true);
    setError(null);
    try {
      const [items, nextDraft, nextStatus, engineList] = await Promise.all([
        api.listWorkflows(), api.getManagedWorkflowDraft(workflowId), api.getWorkflowStatus(workflowId), api.listEngines({ limit: 200 }),
      ]);
      const item = items.find((candidate) => candidate.id === workflowId);
      if (!item) throw new Error("工作流不存在");
      if (nextDraft.definition.schema_version !== 2) throw new Error("该工作流仍使用旧版自由 DAG，请在清空数据库后重新创建");
      setWorkflow(item);
      setDraft(nextDraft);
      setDefinition(nextDraft.definition);
      revisionRef.current = nextDraft.revision;
      setStatus(nextStatus);
      setEngines(engineList.items);
      setSaveState("idle");
      if (api.listWorkflowRevisions) setRevisions(await api.listWorkflowRevisions(workflowId));
    } catch (reason) {
      setError(errorMessage(reason, reason instanceof Error ? reason.message : "工作流读取失败。"));
    } finally { setLoading(false); }
  }, [api, workflowId]);

  useEffect(() => { const timer = window.setTimeout(() => void load(), 0); return () => window.clearTimeout(timer); }, [load, refreshVersion]);

  useEffect(() => {
    if (!definition || saveState !== "dirty" || !api.saveManagedWorkflowDraft) return;
    const capturedVersion = changeVersion.current;
    const timer = window.setTimeout(async () => {
      setSaveState("saving");
      try {
        const next = await api.saveManagedWorkflowDraft!(workflowId, definition, revisionRef.current);
        revisionRef.current = next.revision;
        setDraft(next);
        if (capturedVersion === changeVersion.current) setSaveState("saved");
        else setSaveState("dirty");
        if (api.getWorkflowStatus) setStatus(await api.getWorkflowStatus(workflowId));
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 409) setSaveState("conflict");
        else { setSaveState("error"); setError(errorMessage(reason, "草稿自动保存失败。")); }
      }
    }, 800);
    return () => window.clearTimeout(timer);
  }, [api, definition, saveState, workflowId]);

  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (saveState === "dirty" || saveState === "saving") { event.preventDefault(); event.returnValue = ""; }
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [saveState]);

  const onDefinitionChange = (next: StageWorkflowDefinition) => {
    changeVersion.current += 1;
    setDefinition(next);
    setSaveState("dirty");
  };

  const publishBlocked = useMemo(() => {
    if (saveState === "dirty" || saveState === "saving") return "请等待草稿自动保存完成";
    if (saveState === "conflict") return "请先解决草稿版本冲突";
    if (status?.diagnostics.length) return "请先修复全部配置问题";
    if (status?.stages.some((stage) => !stage.engine_id)) return "每个必需阶段都必须绑定引擎";
    return null;
  }, [saveState, status]);

  const publish = async () => {
    if (!api.publishWorkflow || publishBlocked) return;
    setPublishing(true); setError(null);
    try {
      await api.publishWorkflow(workflowId, revisionRef.current);
      setPublishConfirmOpen(false);
      await load();
      navigate(`/workflows/${encodeURIComponent(workflowId)}/test`);
    } catch (reason) { setError(errorMessage(reason, "发布失败，当前运行版本未改变。")); }
    finally { setPublishing(false); }
  };

  const restore = async (revision: number) => {
    if (!api.restoreWorkflowRevision) return;
    try {
      await api.restoreWorkflowRevision(workflowId, revision, revisionRef.current);
      await load();
      navigate(`/workflows/${encodeURIComponent(workflowId)}/build`);
    } catch (reason) { setError(errorMessage(reason, "历史版本恢复失败。")); }
  };

  if (loading) return <main className="workspace-page"><div className="empty-state">正在加载工作流…</div></main>;
  if (error && !workflow) return <main className="workspace-page"><div className="alert alert-error">{error}</div><Link to="/workflows">返回工作流</Link></main>;
  if (!workflow || !draft || !definition || !status) return null;
  const readyStages = status.stages.filter((stage) => stage.engine_id && stage.health === "healthy").length;
  const publishLabel = status.active_revision === null ? "首次发布" : status.has_unpublished_changes ? "更新运行版本" : "重新发布当前草稿";

  return (
    <main className="workspace-page workflow-detail-page">
      <header className="workflow-heading">
        <div><Link className="back-link" to="/workflows">← 全部工作流</Link><div className="title-line"><h1>{workflow.name}</h1><span className={`status-chip status-${status.state}`}>{stateText[status.state] ?? status.state}</span></div><p><code>{workflow.slug}</code> · {workflow.kind === "voice_agent" ? "Voice Agent" : "实时转写"} · 草稿 r{draft.revision}{status.active_revision ? ` · 运行 v${status.active_revision}` : ""}</p></div>
        <div className={`save-state save-${saveState}`}>{saveState === "saving" ? "正在保存…" : saveState === "dirty" ? "等待自动保存" : saveState === "saved" ? "草稿已保存" : saveState === "conflict" ? "版本冲突" : saveState === "error" ? "保存失败" : "草稿已同步"}</div>
      </header>
      <nav className="detail-tabs" aria-label="工作流详情导航">{tabs.map(([key, label]) => <NavLink key={key} to={`/workflows/${encodeURIComponent(workflowId)}/${key}`} className={tab === key ? "active" : ""}>{label}</NavLink>)}</nav>
      {error && <div className="alert alert-error">{error}<button onClick={() => setError(null)}>关闭</button></div>}
      {saveState === "conflict" && <div className="alert alert-warning"><div><strong>草稿已被其他页面修改</strong><p>为避免覆盖他人修改，自动保存已经停止。</p></div><button className="secondary-button" onClick={() => void load()}>加载服务器版本</button></div>}

      {tab === "overview" && <div className="overview-grid">
        <section className="panel overview-primary"><div className="panel-heading"><div><h2>上线准备度</h2><p>发布、接入和运行健康互相独立显示。</p></div><strong className="readiness-score">{status.diagnostics.length === 0 && status.stages.every((stage) => stage.engine_id) ? "配置完整" : "需要处理"}</strong></div>
          <div className="checklist">{status.stages.map((stage) => <div className="check-row" key={stage.stage}><span className={stage.engine_id ? "check-ok" : "check-missing"}>{stage.engine_id ? "✓" : "!"}</span><div><strong>{stage.stage.replaceAll("_", " ")}</strong><small>{stage.engine_name ?? stage.reason ?? "未绑定引擎"}</small></div><span className={`health-text health-${stage.health}`}>{stage.health}</span></div>)}</div>
        </section>
        <aside className="panel access-card"><h2>接入信息</h2><label><span>公开模型名</span><code>{status.public_models[0] ?? workflow.slug}</code></label><label><span>Realtime WSS</span><code>/v1/realtime?model={workflow.slug}</code></label><label><span>Realtime WebRTC</span><code>/v1/realtime/calls?model={workflow.slug}</code></label>{workflow.kind === "transcription" && <label><span>gRPC</span><code>grpc-default</code></label>}</aside>
      </div>}

      {tab === "build" && <StageBuilder definition={definition} kind={workflow.kind} engines={engines} onChange={onDefinitionChange} />}
      {tab === "test" && <RealtimeTestPanel model={workflow.slug} workflowKind={workflow.kind} canTest={status.active_revision !== null} blockedReason="请先发布一个可运行版本，再开始端到端测试。" />}
      {tab === "publish" && <div className="publish-layout">
        <section className="panel publish-review" aria-labelledby="publish-review-title"><div className="panel-heading"><div><p className="eyebrow">RELEASE REVIEW</p><h2 id="publish-review-title">{publishLabel}</h2><p>激活前会重新编译草稿，并准备所有已绑定引擎；任何一步失败都会保留当前运行版本。</p></div><span className={`status-chip status-${status.state}`}>{stateText[status.state] ?? status.state}</span></div>
          <dl className="publish-summary"><div><dt>当前运行版本</dt><dd>{status.active_revision ? `v${status.active_revision}` : "尚未发布"}</dd></div><div><dt>即将激活草稿</dt><dd>r{draft.revision}</dd></div><div><dt>公开模型名</dt><dd><code>{status.public_models[0] ?? workflow.slug}</code></dd></div><div><dt>已就绪阶段</dt><dd>{readyStages} / {status.stages.length}</dd></div></dl>
        </section>
        <section className="panel publish-stages" aria-labelledby="publish-stages-title"><div className="panel-heading"><div><h2 id="publish-stages-title">运行阶段快照</h2><p>以下绑定会被编译进下一次激活；状态来自最近一次引擎探测。</p></div></div><div className="publish-stage-list">{status.stages.map((stage) => <div className="publish-stage-row" key={stage.stage}><div><strong>{stage.stage.replaceAll("_", " ")}</strong><small>{stage.engine_name ?? "未绑定引擎"}</small></div><div><span className={`health-text health-${stage.health}`}>{healthText[stage.health] ?? stage.health}</span>{stage.reason && <small>{stage.reason}</small>}</div></div>)}</div></section>
        <section className="panel publish-checks" aria-labelledby="publish-checks-title"><div className="panel-heading"><div><h2 id="publish-checks-title">发布检查</h2><p>完成所有阻断项后才能进入确认。</p></div></div>{status.diagnostics.length ? <div className="diagnostic-list">{status.diagnostics.map((item, index) => <button key={`${item.code}-${index}`} onClick={() => item.stage && navigate(`/workflows/${encodeURIComponent(workflowId)}/build#stage-${item.stage}`)}><strong>{item.message}</strong><small>{item.code} · 前往构建修复</small></button>)}</div> : <div className="success-block">结构与字段校验通过。发布时仍会重新准备全部引擎。</div>}
          <div className="panel-actions"><button className="primary-button large-button" disabled={Boolean(publishBlocked) || publishing} title={publishBlocked ?? undefined} onClick={() => setPublishConfirmOpen(true)}>{publishing ? "正在探测并发布…" : "检查并发布"}</button>{publishBlocked && <span className="blocking-reason">{publishBlocked}</span>}</div>
        </section>
        <AlertDialog.Root open={publishConfirmOpen} onOpenChange={setPublishConfirmOpen}><AlertDialog.Portal><AlertDialog.Overlay className="drawer-backdrop" /><AlertDialog.Content className="confirm-dialog"><AlertDialog.Title>确认{publishLabel}？</AlertDialog.Title><AlertDialog.Description>草稿 r{draft.revision} 将在全部引擎准备成功后成为新的运行版本。若检查或激活失败，当前运行版本不会改变。</AlertDialog.Description><dl className="publish-confirm-summary"><div><dt>当前</dt><dd>{status.active_revision ? `v${status.active_revision}` : "尚未发布"}</dd></div><div><dt>目标</dt><dd>草稿 r{draft.revision}</dd></div></dl><div className="panel-actions"><AlertDialog.Cancel className="secondary-button" disabled={publishing}>返回检查</AlertDialog.Cancel><AlertDialog.Action className="primary-button" disabled={publishing} onClick={(event) => { event.preventDefault(); void publish(); }}>{publishing ? "正在发布…" : "确认发布"}</AlertDialog.Action></div></AlertDialog.Content></AlertDialog.Portal></AlertDialog.Root>
      </div>}
      {tab === "history" && <section className="panel"><div className="panel-heading"><div><h2>发布历史</h2><p>历史版本只能恢复为新草稿，不直接改变活动会话。</p></div></div>{revisions.length === 0 ? <div className="empty-state">尚无发布历史</div> : <div className="revision-list">{revisions.map((revision) => <div className="revision-row" key={revision.revision}><div><strong>运行版本 v{revision.revision}</strong><small>{new Date(revision.activated_at).toLocaleString("zh-CN")}</small></div><span>{Object.keys(revision.engine_revisions).length} 个引擎快照</span><button className="secondary-button" onClick={() => void restore(revision.revision)}>恢复为新草稿</button></div>)}</div>}</section>}
    </main>
  );
}
