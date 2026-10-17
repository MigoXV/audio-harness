import { useCallback, useEffect, useMemo, useState } from "react";

import { errorMessage } from "../api/client";
import type {
  AgentBackend,
  AgentBackendCapabilities,
  AgentBackendCreate,
  AudioHarnessApi,
} from "../api/types";
import { normalizeSlug } from "../features/forms/slug";

interface Props { api: AudioHarnessApi; refreshVersion: number; }

function blankBackend(): AgentBackendCreate {
  return {
    slug: "",
    name: "",
    kind: "dsh",
    protocol: "dsh_web_v1",
    target: "http://127.0.0.1:3080",
    connect_timeout_seconds: 10,
    request_timeout_seconds: 30,
    headers_env: {},
    enabled: true,
    description: null,
    details: {},
  };
}

export function serializeHeadersEnv(headers: Record<string, string>): string {
  return Object.entries(headers).map(([header, envName]) => `${header}=${envName}`).join("\n");
}

export function parseHeadersEnv(value: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [index, rawLine] of value.split("\n").entries()) {
    const line = rawLine.trim();
    if (!line) continue;
    const separator = line.indexOf("=");
    if (separator <= 0 || !line.slice(separator + 1).trim()) {
      throw new Error(`请求头环境变量第 ${index + 1} 行应为 Header=ENV_NAME`);
    }
    result[line.slice(0, separator).trim()] = line.slice(separator + 1).trim();
  }
  return result;
}

function statusLabel(status: AgentBackend["status"]): string {
  return ({ healthy: "健康", unhealthy: "不可用", unknown: "未探测", disabled: "已停用" } as const)[status];
}

export function AgentBackendPage({ api, refreshVersion }: Props) {
  const [items, setItems] = useState<AgentBackend[]>([]);
  const [editing, setEditing] = useState<AgentBackend | "new" | null>(null);
  const [values, setValues] = useState<AgentBackendCreate>(blankBackend());
  const [headersText, setHeadersText] = useState("");
  const [capabilities, setCapabilities] = useState<Record<string, AgentBackendCapabilities>>({});
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [slugEdited, setSlugEdited] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!api.listAgentBackends) {
      setError("当前 API 客户端不支持 Agent Backend 管理。");
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      setItems(await api.listAgentBackends());
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Agent Backend 列表读取失败。"));
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, refreshVersion]);

  const counts = useMemo(() => ({
    healthy: items.filter((item) => item.status === "healthy").length,
    unhealthy: items.filter((item) => item.status === "unhealthy").length,
    disabled: items.filter((item) => !item.enabled).length,
  }), [items]);

  const openCreate = () => {
    setEditing("new");
    setValues(blankBackend());
    setHeadersText("");
    setSlugEdited(false);
    setError(null);
  };

  const openEdit = (backend: AgentBackend) => {
    setEditing(backend);
    setValues({
      slug: backend.slug,
      name: backend.name,
      kind: backend.kind,
      protocol: backend.protocol,
      target: backend.target,
      connect_timeout_seconds: backend.connect_timeout_seconds,
      request_timeout_seconds: backend.request_timeout_seconds,
      headers_env: backend.headers_env,
      enabled: backend.enabled,
      description: backend.description,
      details: backend.details,
    });
    setHeadersText(serializeHeadersEnv(backend.headers_env));
    setSlugEdited(true);
    setError(null);
  };

  const save = async () => {
    if (!api.createAgentBackend || !api.updateAgentBackend) return;
    setBusy(true);
    setError(null);
    try {
      const payload = { ...values, headers_env: parseHeadersEnv(headersText) };
      if (editing === "new") await api.createAgentBackend(payload);
      else if (editing) await api.updateAgentBackend(editing.id, payload);
      setEditing(null);
      await load();
    } catch (reason) {
      setError(errorMessage(reason, "Agent Backend 保存失败。"));
    } finally {
      setBusy(false);
    }
  };

  const probe = async (backend: AgentBackend) => {
    if (!api.validateAgentBackend || !api.getAgentBackendCapabilities) return;
    setBusy(true);
    setError(null);
    try {
      const validation = await api.validateAgentBackend(backend.id);
      if (validation.outcome !== "passed") throw new Error(validation.message ?? "DSH 探测失败");
      const next = await api.getAgentBackendCapabilities(backend.id);
      setCapabilities((current) => ({ ...current, [backend.id]: next }));
      setExpanded((current) => new Set(current).add(backend.id));
      await load();
    } catch (reason) {
      setError(errorMessage(reason, "DSH 探测或能力读取失败。"));
      await load();
    } finally {
      setBusy(false);
    }
  };

  const remove = async (backend: AgentBackend) => {
    if (!api.deleteAgentBackend || !window.confirm(`删除“${backend.name}”？被 Voice Agent 引用时服务会拒绝操作。`)) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteAgentBackend(backend.id);
      await load();
    } catch (reason) {
      setError(errorMessage(reason, "Agent Backend 删除失败。"));
    } finally {
      setBusy(false);
    }
  };

  const toggleEnabled = async (backend: AgentBackend) => {
    if (!api.updateAgentBackend) return;
    setBusy(true);
    try {
      await api.updateAgentBackend(backend.id, { enabled: !backend.enabled });
      await load();
    } catch (reason) {
      setError(errorMessage(reason, backend.enabled ? "Agent Backend 停用失败。" : "Agent Backend 启用失败。"));
    } finally {
      setBusy(false);
    }
  };

  return <main className="workspace-page agent-backend-page">
    <header className="page-heading"><div><h1>智能体后端</h1><p>把 DSH 作为与 Engine 平级的一等资源管理；凭据只通过运行环境注入，不保存真实密钥。</p></div><button className="primary-button" onClick={openCreate}>注册 DSH</button></header>
    {error && <div className="alert alert-error" role="alert">{error}<button className="text-button" onClick={() => setError(null)}>关闭</button></div>}
    <section className="engine-health-summary" aria-label="智能体后端概览"><div><span>全部</span><strong>{items.length}</strong></div><div><span>健康</span><strong>{counts.healthy}</strong></div><div><span>异常</span><strong>{counts.unhealthy}</strong></div><div><span>停用</span><strong>{counts.disabled}</strong></div></section>
    <section className="panel engine-accordion-list" aria-label="智能体后端列表">
      {loading && items.length === 0 ? <div className="empty-state">正在加载 Agent Backends…</div> : items.length === 0 ? <div className="empty-state">尚未注册 Agent Backend</div> : items.map((backend) => {
        const open = expanded.has(backend.id);
        const capability = capabilities[backend.id];
        return <article className={`engine-card ${open ? "expanded" : "collapsed"}`} key={backend.id}>
          <header className="engine-card-summary"><button className="engine-card-toggle" aria-expanded={open} onClick={() => setExpanded((current) => { const next = new Set(current); if (next.has(backend.id)) next.delete(backend.id); else next.add(backend.id); return next; })}><span><strong>{backend.name}</strong><small>{backend.slug} · DSH Web v1 · r{backend.revision}</small></span><code>{backend.target}</code><span className={`status-chip engine-${backend.status}`}>{statusLabel(backend.status)}</span><i aria-hidden="true">⌄</i></button><button className={backend.enabled ? "danger-text engine-lifecycle-button" : "secondary-button engine-lifecycle-button"} disabled={busy} onClick={() => void toggleEnabled(backend)}>{backend.enabled ? "停用" : "启用"}</button></header>
          {open && <div className="engine-card-details"><dl><div><dt>最近探测</dt><dd>{backend.status_observed_at ? new Date(backend.status_observed_at).toLocaleString("zh-CN") : "尚未探测"}</dd></div><div><dt>探测延迟</dt><dd>{backend.probe_latency_ms == null ? "—" : `${Math.round(backend.probe_latency_ms)} ms`}</dd></div><div><dt>请求头环境变量</dt><dd>{Object.keys(backend.headers_env).length || "无"}</dd></div><div><dt>连续失败</dt><dd>{backend.consecutive_failures}</dd></div></dl>{backend.status_basis && <p className="engine-status-reason"><strong>状态说明</strong>{backend.status_basis}</p>}{capability && <section className="backend-capabilities"><h3>DSH 能力</h3><dl><div><dt>Provider / Model</dt><dd>{capability.provider} / {capability.model}</dd></div><div><dt>默认 cwd</dt><dd><code>{capability.cwd}</code></dd></div><div><dt>Wire</dt><dd>{capability.wire_version}</dd></div><div><dt>插件</dt><dd>{capability.plugins.filter((plugin) => plugin.enabled).length} / {capability.plugins.length}</dd></div></dl><p>Presets：{capability.presets.join("、") || "无"}</p></section>}<div className="row-actions"><button disabled={busy || !backend.enabled} onClick={() => void probe(backend)}>探测并读取能力</button><button onClick={() => openEdit(backend)}>编辑</button><button className="danger-text" onClick={() => void remove(backend)}>删除</button></div></div>}
        </article>;
      })}
    </section>
    {editing && <section className="panel backend-editor" aria-label={editing === "new" ? "注册 DSH" : "编辑 DSH"}><div className="panel-heading"><div><p className="eyebrow">DSH WEB V1</p><h2>{editing === "new" ? "注册 DSH" : `编辑 ${editing.name}`}</h2><p>target 同时派生 HTTP JSON-RPC 与 WebSocket 地址；远端部署应使用私网或鉴权代理。</p></div><button className="icon-button" onClick={() => setEditing(null)} aria-label="关闭">×</button></div><div className="form-grid two-columns"><label><span>名称</span><input value={values.name} onChange={(event) => { const name = event.target.value; setValues((current) => ({ ...current, name, slug: editing === "new" && !slugEdited ? normalizeSlug(name) : current.slug })); }} /></label><label><span>Slug</span><input value={values.slug} onChange={(event) => { setSlugEdited(true); setValues({ ...values, slug: normalizeSlug(event.target.value) }); }} /></label><label className="full-span"><span>DSH Target</span><input value={values.target} onChange={(event) => setValues({ ...values, target: event.target.value })} placeholder="http://dsh.internal:3080" /></label><label><span>连接超时（秒）</span><input type="number" min="0.1" max="120" value={values.connect_timeout_seconds} onChange={(event) => setValues({ ...values, connect_timeout_seconds: Number(event.target.value) })} /></label><label><span>请求超时（秒）</span><input type="number" min="0.1" max="600" value={values.request_timeout_seconds} onChange={(event) => setValues({ ...values, request_timeout_seconds: Number(event.target.value) })} /></label><label className="full-span"><span>请求头 → 环境变量</span><textarea rows={4} value={headersText} onChange={(event) => setHeadersText(event.target.value)} placeholder={"Authorization=DSH_AUTH_HEADER\nX-API-Key=DSH_API_KEY"} /><small>每行 Header=ENV_NAME；数据库只保存环境变量名。</small></label><label className="full-span"><span>描述</span><textarea value={values.description ?? ""} onChange={(event) => setValues({ ...values, description: event.target.value || null })} /></label><label className="switch-control full-span"><input type="checkbox" checked={values.enabled} onChange={(event) => setValues({ ...values, enabled: event.target.checked })} /><span>启用并参与健康探测</span></label></div><div className="panel-actions"><button className="secondary-button" disabled={busy} onClick={() => setEditing(null)}>取消</button><button className="primary-button" disabled={busy || !values.name.trim() || !values.slug || !values.target.trim()} onClick={() => void save()}>{busy ? "保存中…" : "保存 Agent Backend"}</button></div></section>}
  </main>;
}
