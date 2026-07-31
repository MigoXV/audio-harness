import { useEffect, useMemo, useRef, useState } from "react";

import type {
  EngineAsset,
  FailurePolicy,
  NodeTypeDefinition,
  PipelineNode,
} from "../api/types";
import {
  nodeDisplayName,
  protocolLabel,
} from "../model";

interface JsonSchemaProperty {
  title?: string;
  description?: string;
  type?: string;
  default?: unknown;
  minimum?: number;
  exclusiveMinimum?: number;
}

interface NodeConfigDrawerProps {
  node: PipelineNode;
  nodeType: NodeTypeDefinition;
  engine: EngineAsset | null;
  onChange: (node: PipelineNode) => void;
  onPickEngine: () => void;
  onClose: () => void;
}

const CONFIG_LABELS: Record<string, string> = {
  min_segment_seconds: "最短语音段",
  max_segment_seconds: "最长语音段",
  audio_buffer_seconds: "轮转音频缓冲",
  speech_pre_roll_seconds: "语音起点前置",
};

const CONFIG_HINTS: Record<string, string> = {
  min_segment_seconds: "短于该时长的片段不会进入下游节点。",
  max_segment_seconds: "超长发言按该窗口形成语音片段。",
  audio_buffer_seconds: "需要覆盖最长语音段和起点前置音频。",
  speech_pre_roll_seconds: "为语音起点补回少量前文，避免开头缺字。",
};

function schemaProperties(
  schema: Record<string, unknown>,
): Array<[string, JsonSchemaProperty]> {
  const properties = schema.properties;
  if (
    properties === null ||
    typeof properties !== "object" ||
    Array.isArray(properties)
  ) {
    return [];
  }
  return Object.entries(properties).map(([key, value]) => [
    key,
    value !== null && typeof value === "object" && !Array.isArray(value)
      ? (value as JsonSchemaProperty)
      : {},
  ]);
}

function failurePolicyLabel(policy: FailurePolicy): string {
  return policy === "fail_open" ? "失败时跳过节点" : "失败时终止处理链";
}

export function NodeConfigDrawer({
  node,
  nodeType,
  engine,
  onChange,
  onPickEngine,
  onClose,
}: NodeConfigDrawerProps) {
  const [draft, setDraft] = useState(node);
  const closeRef = useRef<HTMLButtonElement>(null);
  const properties = useMemo(
    () => schemaProperties(nodeType.config_schema),
    [nodeType.config_schema],
  );

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  const updateConfig = (key: string, value: unknown) => {
    setDraft((current) => ({
      ...current,
      config: { ...current.config, [key]: value },
    }));
  };

  return (
    <div
      className="drawer-backdrop"
      role="presentation"
      onMouseDown={onClose}
    >
      <aside
        className="node-config-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="node-config-title"
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            onClose();
          }
        }}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="drawer-heading">
          <div>
            <h2 id="node-config-title">配置{nodeDisplayName(node, [nodeType])}</h2>
            <p>
              {nodeType.display_name} · <code>{node.id}</code>
            </p>
          </div>
          <button
            ref={closeRef}
            className="drawer-close"
            type="button"
            aria-label="关闭节点配置"
            onClick={onClose}
          >
            ×
          </button>
        </header>

        <div className="node-config-content">
          <section className="node-config-section">
            <h3>基本信息</h3>
            <label className="form-field">
              <span>节点名称</span>
              <input
                value={draft.display_name ?? ""}
                placeholder={nodeType.display_name}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    display_name: event.target.value.trimStart() || null,
                  })
                }
              />
            </label>

            {nodeType.allowed_failure_policies.length > 1 && (
              <label className="form-field">
                <span>失败策略</span>
                <select
                  value={
                    draft.failure_policy ?? nodeType.default_failure_policy
                  }
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      failure_policy: event.target.value as FailurePolicy,
                    })
                  }
                >
                  {nodeType.allowed_failure_policies.map((policy) => (
                    <option key={policy} value={policy}>
                      {failurePolicyLabel(policy)}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </section>

          {nodeType.required_api && (
            <section className="node-config-section">
              <h3>运行引擎</h3>
              {engine ? (
                <div className="node-config-engine">
                  <strong>{engine.name}</strong>
                  <code>{engine.target}</code>
                  <span>
                    {protocolLabel(engine.protocol)} · {engine.api}
                  </span>
                </div>
              ) : (
                <p className="node-config-empty">当前尚未选择引擎。</p>
              )}
              <button
                className="text-button"
                type="button"
                onClick={onPickEngine}
              >
                {engine ? "更换引擎" : "选择引擎"}
              </button>
            </section>
          )}

          {properties.length > 0 && (
            <section className="node-config-section">
              <h3>节点参数</h3>
              {properties.map(([key, property]) => {
                const value =
                  draft.config[key] ?? property.default ?? "";
                const label = CONFIG_LABELS[key] ?? property.title ?? key;
                const hint =
                  CONFIG_HINTS[key] ?? property.description ?? "";
                if (property.type === "boolean") {
                  return (
                    <label className="node-config-toggle" key={key}>
                      <span>
                        <strong>{label}</strong>
                        {hint && <small>{hint}</small>}
                      </span>
                      <input
                        type="checkbox"
                        checked={Boolean(value)}
                        onChange={(event) =>
                          updateConfig(key, event.target.checked)
                        }
                      />
                    </label>
                  );
                }
                return (
                  <label className="form-field" key={key}>
                    <span>{label}</span>
                    <input
                      type={
                        property.type === "number" ||
                        property.type === "integer"
                          ? "number"
                          : "text"
                      }
                      min={
                        property.minimum ??
                        (property.exclusiveMinimum !== undefined
                          ? property.exclusiveMinimum
                          : undefined)
                      }
                      step={property.type === "integer" ? 1 : "any"}
                      value={String(value)}
                      onChange={(event) =>
                        updateConfig(
                          key,
                          property.type === "number" ||
                            property.type === "integer"
                            ? Number(event.target.value)
                            : event.target.value,
                        )
                      }
                    />
                    {hint && <small>{hint}</small>}
                  </label>
                );
              })}
            </section>
          )}
        </div>

        <footer className="drawer-actions">
          <span>修改会先写入本地工作副本。</span>
          <div>
            <button
              className="button button-quiet"
              type="button"
              onClick={onClose}
            >
              取消
            </button>
            <button
              className="button button-primary"
              type="button"
              onClick={() => {
                onChange(draft);
                onClose();
              }}
            >
              应用节点配置
            </button>
          </div>
        </footer>
      </aside>
    </div>
  );
}
