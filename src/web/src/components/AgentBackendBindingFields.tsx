import { useEffect, useState } from "react";

import { errorMessage } from "../api/client";
import type {
  AgentBackend,
  AgentBackendCapabilities,
  AudioHarnessApi,
  VoiceAgentDefinition,
} from "../api/types";

interface Props {
  api: AudioHarnessApi;
  backends: AgentBackend[];
  definition: VoiceAgentDefinition;
  contextPreview?: string | null;
  contextPreviewStale?: boolean;
  onChange: (definition: VoiceAgentDefinition) => void;
}

export function createAgentBackendBinding(backendId: string, cwd = "") {
  return {
    backend_id: backendId,
    cwd,
    agent_preset: "standard",
    coordinator_instructions: "",
    task_timeout_seconds: 900,
    interaction_timeout_seconds: 120,
  };
}

export function AgentBackendBindingFields({ api, backends, definition, contextPreview, contextPreviewStale = false, onChange }: Props) {
  const binding = definition.agent_backend ?? null;
  const [capabilities, setCapabilities] = useState<AgentBackendCapabilities | null>(null);
  const [capabilityError, setCapabilityError] = useState<{ backendId: string; message: string } | null>(null);
  const backendId = binding?.backend_id ?? null;

  useEffect(() => {
    if (!backendId || !api.getAgentBackendCapabilities) return;
    const controller = new AbortController();
    void api.getAgentBackendCapabilities(backendId, controller.signal).then((next) => {
      setCapabilities(next);
      setCapabilityError(null);
    }).catch((reason) => {
      if (!controller.signal.aborted) setCapabilityError({ backendId, message: errorMessage(reason, "DSH 能力读取失败。") });
    });
    return () => controller.abort();
  }, [api, backendId]);

  const changeBinding = (patch: Partial<NonNullable<VoiceAgentDefinition["agent_backend"]>>) => {
    if (!binding) return;
    onChange({ ...definition, schema_version: 2, agent_backend: { ...binding, ...patch } });
  };
  const selected = backends.find((item) => item.id === binding?.backend_id);
  const activeCapabilities = capabilities?.backend_id === backendId ? capabilities : null;
  const presets = activeCapabilities?.presets ?? [];
  const activeCapabilityError = capabilityError?.backendId === backendId ? capabilityError.message : null;

  return <section className="full-span agent-backend-binding">
    <div className="nested-heading"><div><h3>Agent Backend（可选）</h3><p>Responses 仍负责语音工作流；绑定后可把执行任务委派给 DSH。</p></div>{binding && <span className={`status-chip engine-${selected?.status ?? "unknown"}`}>{selected ? `${selected.name} · ${selected.status}` : "后端不存在"}</span>}</div>
    <div className="form-grid two-columns">
      <label className="full-span"><span>智能体后端</span><select value={binding?.backend_id ?? ""} onChange={(event) => {
        const backendId = event.target.value;
        onChange({ ...definition, schema_version: 2, agent_backend: backendId ? createAgentBackendBinding(backendId) : null });
      }}><option value="">不绑定（Responses-only）</option>{backends.filter((item) => item.enabled).map((backend) => <option key={backend.id} value={backend.id}>{backend.name} · {backend.protocol} · {backend.status}</option>)}</select></label>
      {binding && <><label className="full-span"><span>DSH 工作目录（绝对路径）</span><input value={binding.cwd} onChange={(event) => changeBinding({ cwd: event.target.value })} placeholder="/workspace/project" /><small>路径由远端 DSH 容器解释，不是 Audio Harness 容器路径。{activeCapabilities?.cwd && <> DSH 默认：<button type="button" className="text-button" onClick={() => changeBinding({ cwd: activeCapabilities.cwd })}>{activeCapabilities.cwd}</button></>}</small></label><label><span>Agent preset</span><select value={binding.agent_preset} onChange={(event) => changeBinding({ agent_preset: event.target.value })}>{presets.length === 0 && <option value={binding.agent_preset}>{binding.agent_preset}</option>}{presets.map((preset) => <option key={preset} value={preset}>{preset}</option>)}</select></label><label><span>任务超时（秒）</span><input type="number" min="1" max="86400" value={binding.task_timeout_seconds} onChange={(event) => changeBinding({ task_timeout_seconds: Number(event.target.value) })} /></label><label><span>审批/提问超时（秒）</span><input type="number" min="1" max="3600" value={binding.interaction_timeout_seconds} onChange={(event) => changeBinding({ interaction_timeout_seconds: Number(event.target.value) })} /></label><label className="full-span"><span>协调器指令</span><textarea value={binding.coordinator_instructions} onChange={(event) => changeBinding({ coordinator_instructions: event.target.value })} placeholder="补充何时委派、哪些任务必须先确认等规则。" /></label></>}
    </div>
    {activeCapabilityError && <div className="alert alert-warning">{activeCapabilityError}</div>}
    {activeCapabilities && <p className="backend-capability-summary">DSH {activeCapabilities.provider} / {activeCapabilities.model} · {activeCapabilities.plugins.filter((plugin) => plugin.enabled).length} 个已启用插件 · backend r{activeCapabilities.backend_revision}</p>}
    {binding && <section className="agent-backend-context-preview"><h4>自动注入的系统上下文</h4><p>该内容由服务端在发布时冻结，Responses 与协调器都会使用；运行时仅额外叠加会话状态。</p>{contextPreview ? <pre>{contextPreview}</pre> : <p className="empty-copy">保存草稿后生成预览。</p>}{contextPreviewStale && <small>草稿包含未保存修改；请保存后刷新此预览。</small>}</section>}
  </section>;
}
