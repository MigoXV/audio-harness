import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { errorMessage } from "../api/client";
import type {
  AudioHarnessApi,
  WorkflowKind,
  WorkflowStatus,
  WorkflowSummary,
} from "../api/types";

interface Props {
  api: AudioHarnessApi;
  refreshVersion: number;
}

const stateLabels: Record<string, string> = {
  incomplete: "配置未完成",
  unpublished: "待首次发布",
  unpublished_changes: "有未发布修改",
  published_healthy: "运行健康",
  published_degraded: "运行异常",
};

export function WorkflowListPage({ api, refreshVersion }: Props) {
  const navigate = useNavigate();
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [statuses, setStatuses] = useState<Record<string, WorkflowStatus>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [kind, setKind] = useState<WorkflowKind>("voice_agent");

  const load = useCallback(async () => {
    if (!api.listWorkflows || !api.getWorkflowStatus) return;
    setLoading(true);
    setError(null);
    try {
      const items = await api.listWorkflows();
      setWorkflows(items);
      const results = await Promise.allSettled(
        items.map((item) => api.getWorkflowStatus!(item.id)),
      );
      const next: Record<string, WorkflowStatus> = {};
      results.forEach((result, index) => {
        if (result.status === "fulfilled") next[items[index].id] = result.value;
      });
      setStatuses(next);
    } catch (reason) {
      setError(errorMessage(reason, "工作流列表读取失败。"));
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, refreshVersion]);

  const slugPreview = useMemo(
    () => slug.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, ""),
    [slug],
  );

  const create = async () => {
    if (!api.createManagedWorkflow || !name.trim() || !slugPreview) return;
    setCreating(true);
    setError(null);
    try {
      const draft = await api.createManagedWorkflow({
        name: name.trim(),
        slug: slugPreview,
        kind,
        description: null,
      });
      navigate(`/workflows/${encodeURIComponent(draft.workflow_id)}/build`);
    } catch (reason) {
      setError(errorMessage(reason, "工作流创建失败。"));
    } finally {
      setCreating(false);
    }
  };

  return (
    <main className="workspace-page">
      <header className="page-heading">
        <div>
          <p className="eyebrow">WORKFLOWS</p>
          <h1>实时语音工作流</h1>
          <p>从引擎绑定到发布测试，在同一个工作流中完成。</p>
        </div>
        <button className="primary-button" onClick={() => setCreating(true)}>
          新建工作流
        </button>
      </header>

      {error && <div className="alert alert-error">{error}</div>}

      {creating && (
        <section className="panel create-workflow-panel" aria-label="创建工作流">
          <div className="panel-heading">
            <div><h2>创建工作流</h2><p>公开模型名将自动使用不可变 Slug。</p></div>
            <button className="text-button" onClick={() => setCreating(false)}>取消</button>
          </div>
          <div className="form-grid three-columns">
            <label><span>名称</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="客服语音 Agent" /></label>
            <label><span>Slug / 公开模型名</span><input value={slug} onChange={(event) => setSlug(event.target.value)} placeholder="customer-service-agent" /><small>{slugPreview || "仅支持小写字母、数字和连字符"}</small></label>
            <label><span>类型</span><select value={kind} onChange={(event) => setKind(event.target.value as WorkflowKind)}><option value="voice_agent">Voice Agent</option><option value="transcription">实时转写</option></select></label>
          </div>
          <div className="panel-actions"><button className="primary-button" disabled={!name.trim() || !slugPreview} onClick={() => void create()}>{creating ? "创建并进入构建" : "创建"}</button></div>
        </section>
      )}

      <section className="panel">
        <div className="panel-heading">
          <div><h2>全部工作流</h2><p>{workflows.length} 个工作流</p></div>
          <button className="secondary-button" onClick={() => void load()}>刷新状态</button>
        </div>
        {loading ? <div className="empty-state">正在读取工作流…</div> : workflows.length === 0 ? (
          <div className="empty-state"><strong>还没有工作流</strong><span>新建一个 Voice Agent，开始配置 ASR、大模型和 TTS。</span></div>
        ) : (
          <div className="workflow-table" role="table">
            <div className="workflow-row table-header" role="row"><span>工作流</span><span>接入模型</span><span>草稿 / 运行版本</span><span>状态</span><span /></div>
            {workflows.map((workflow) => {
              const status = statuses[workflow.id];
              return (
                <button className="workflow-row" role="row" key={workflow.id} onClick={() => navigate(`/workflows/${encodeURIComponent(workflow.id)}/overview`)}>
                  <span><strong>{workflow.name}</strong><small>{workflow.kind === "voice_agent" ? "Voice Agent" : "实时转写"}</small></span>
                  <span><code>{status?.public_models[0] ?? workflow.slug}</code></span>
                  <span>草稿 r{workflow.draft_revision} · {workflow.active_revision ? `运行 v${workflow.active_revision}` : "未发布"}</span>
                  <span><span className={`status-chip status-${status?.state ?? "unknown"}`}>{stateLabels[status?.state ?? ""] ?? "正在确认"}</span></span>
                  <span className="row-arrow">→</span>
                </button>
              );
            })}
          </div>
        )}
      </section>
    </main>
  );
}
