import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi, AudioWorkflow } from "../api/types";
import { normalizeSlug } from "../features/forms/slug";

interface Props { api: AudioHarnessApi; refreshVersion: number; }

export function WorkflowListPage({ api, refreshVersion }: Props) {
  const navigate = useNavigate();
  const [items, setItems] = useState<AudioWorkflow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const workflows = await api.listAudioWorkflows();
      setItems(workflows);
    }
    catch (reason) { setError(errorMessage(reason, "语音工作流读取失败。")); }
    finally { setLoading(false); }
  }, [api]);
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, refreshVersion]);

  const normalizedSlug = useMemo(() => normalizeSlug(slug), [slug]);
  const openCreate = () => {
    setName("");
    setSlug("");
    setSlugEdited(false);
    setCreating(true);
  };
  const create = async () => {
    if (!name.trim() || !normalizedSlug) return;
    try {
      const workflow = await api.createAudioWorkflow({
        name: name.trim(), slug: normalizedSlug, description: null, document: null,
      });
      navigate(`/audio-workflows/${encodeURIComponent(workflow.id)}/build`);
    } catch (reason) { setError(errorMessage(reason, "语音工作流创建失败。")); }
  };

  return <main className="workspace-page object-list-page">
    <header className="page-heading"><div><h1>语音推理工作流</h1><p>使用类型化节点、端口与命名出口组织 VAD 和 ASR。</p></div><button className="primary-button" onClick={openCreate}>新建语音工作流</button></header>
    {error && <div className="alert alert-error">{error}</div>}
    {creating && <section className="panel create-workflow-panel"><div className="panel-heading"><div><h2>创建语音工作流</h2><p>新草稿会从最小 Physical VAD → ASR 图开始。</p></div><button className="text-button" onClick={() => setCreating(false)}>取消</button></div><div className="form-grid two-columns"><label><span>名称</span><input value={name} autoFocus onChange={(event) => { const value = event.target.value; setName(value); if (!slugEdited) setSlug(normalizeSlug(value)); }} /></label><label><span>Slug</span><input value={slug} onChange={(event) => { setSlugEdited(true); setSlug(normalizeSlug(event.target.value)); }} /><small>{normalizedSlug || (name.trim() ? "名称无法生成英文 Slug，请手动填写" : "小写字母、数字和连字符")}</small></label></div><div className="panel-actions"><button className="primary-button" disabled={!name.trim() || !normalizedSlug} onClick={() => void create()}>创建并编辑</button></div></section>}
    <section className="panel object-list-panel"><div className="panel-heading"><div><h2>工作流列表</h2><p>{items.length} 个独立推理图；发布与对外接入分别管理。</p></div><button className="secondary-button" onClick={() => void load()}>刷新</button></div>
      {loading ? <div className="empty-state">正在加载…</div> : <div className="workflow-table" role="table">
        <div className="workflow-row table-header"><span>工作流</span><span>Slug</span><span>草稿</span><span>活动版本</span><span>接入</span><span /></div>
        <div className="object-records" tabIndex={0} role="rowgroup" aria-label="工作流记录">{items.map((item) => <button className="workflow-row" key={item.id} onClick={() => navigate(`/audio-workflows/${encodeURIComponent(item.id)}/build`)}><span><strong>{item.name}</strong><small>{item.description ?? "类型化语音 DAG"}</small></span><span><code>{item.slug}</code></span><span>r{item.draft_revision}</span><span><span className={`status-chip ${item.active_revision ? "status-published_healthy" : "status-unpublished"}`}>{item.active_revision ? `v${item.active_revision}` : "未发布"}</span></span><span className="exposure-badges">{item.active_revision ? <code>{item.slug}</code> : <small className="muted">待发布</small>}</span><span className="row-arrow">→</span></button>)}
        </div>
      </div>}
    </section>
  </main>;
}
