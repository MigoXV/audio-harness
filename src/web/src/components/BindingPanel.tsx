import { useState } from "react";

import type {
  EngineAsset,
  EngineBinding,
  EngineRole,
  HealthStatus,
} from "../api/types";
import {
  bindingCandidates,
  formatDate,
  ROLE_LABELS,
} from "../model";

interface BindingPanelProps {
  bindings: EngineBinding[];
  candidates: EngineAsset[];
  health: HealthStatus | null;
  loading: boolean;
  pendingRole: EngineRole | null;
  selectedEngine: EngineAsset | null;
  onBind: (role: EngineRole, engineId: string) => void;
  onUnbind: (binding: EngineBinding) => void;
}

const ROLES: EngineRole[] = ["asr", "vad", "semantic_vad"];

function BindingSection({
  binding,
  engines,
  pending,
  onBind,
  onUnbind,
}: {
  binding: EngineBinding;
  engines: EngineAsset[];
  pending: boolean;
  onBind: (engineId: string) => void;
  onUnbind: () => void;
}) {
  const available = bindingCandidates(binding.role, engines);
  const [choice, setChoice] = useState(binding.engine?.id ?? "");

  return (
    <section className="binding-section">
      <div className="binding-heading">
        <div>
          <h3>{ROLE_LABELS[binding.role]}</h3>
          <span>{binding.role}</span>
        </div>
        <span className={`binding-state ${binding.engine ? "is-bound" : ""}`}>
          {binding.engine ? "已绑定" : "未绑定"}
        </span>
      </div>

      {binding.engine ? (
        <div className="current-binding">
          <strong>{binding.engine.name}</strong>
          <code>{binding.engine.target}</code>
          <span>
            generation {binding.generation} · {formatDate(binding.updated_at)}
          </span>
        </div>
      ) : (
        <p className="binding-empty">此角色尚未接入引擎。</p>
      )}

      <div className="binding-controls">
        <select
          aria-label={`${ROLE_LABELS[binding.role]}候选引擎`}
          value={choice}
          onChange={(event) => setChoice(event.target.value)}
          disabled={pending}
        >
          <option value="">选择启用的兼容引擎</option>
          {available.map((engine) => (
            <option key={engine.id} value={engine.id}>
              {engine.name} · {engine.target}
            </option>
          ))}
        </select>
        <button
          className="button button-secondary"
          type="button"
          disabled={!choice || pending || choice === binding.engine?.id}
          onClick={() => onBind(choice)}
        >
          {pending ? "正在探活…" : binding.engine ? "验证并切换" : "验证并绑定"}
        </button>
        {binding.engine && (
          <button
            className="text-button text-button-danger"
            type="button"
            disabled={pending}
            onClick={onUnbind}
          >
            解除
          </button>
        )}
      </div>
    </section>
  );
}

export function BindingPanel({
  bindings,
  candidates,
  health,
  loading,
  pendingRole,
  selectedEngine,
  onBind,
  onUnbind,
}: BindingPanelProps) {
  return (
    <aside className="side-panel" aria-labelledby="binding-title">
      <div className="section-heading side-heading">
        <div>
          <p className="eyebrow">RUNTIME ROUTING</p>
          <h2 id="binding-title">角色绑定</h2>
        </div>
        <span
          className={`readiness ${
            health?.status === "ready" ? "is-ready" : ""
          }`}
        >
          {loading
            ? "读取中"
            : health?.status === "ready"
              ? "READY"
              : "NOT READY"}
        </span>
      </div>

      <div className="binding-list">
        {ROLES.map((role) => {
          const binding = bindings.find((item) => item.role === role) ?? {
            role,
            engine: null,
            generation: 0,
            updated_at: null,
          };
          return (
            <BindingSection
              key={`${role}:${binding.engine?.id ?? "unbound"}`}
              binding={binding}
              engines={candidates}
              pending={pendingRole === role}
              onBind={(engineId) => onBind(role, engineId)}
              onUnbind={() => onUnbind(binding)}
            />
          );
        })}
      </div>

      {selectedEngine && (
        <section className="selection-context">
          <p className="eyebrow">SELECTED ASSET</p>
          <strong>{selectedEngine.name}</strong>
          <code>{selectedEngine.slug}</code>
          <dl>
            <div>
              <dt>连接超时</dt>
              <dd>{selectedEngine.connect_timeout_seconds}s</dd>
            </div>
            <div>
              <dt>请求超时</dt>
              <dd>{selectedEngine.request_timeout_seconds ?? "不限"}</dd>
            </div>
            <div>
              <dt>来源</dt>
              <dd>{selectedEngine.source || "—"}</dd>
            </div>
          </dl>
        </section>
      )}

      {health && health.problems.length > 0 && (
        <section className="health-problems">
          <p className="eyebrow">READINESS</p>
          <ul>
            {health.problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </section>
      )}
    </aside>
  );
}
