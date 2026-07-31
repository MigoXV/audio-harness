import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import type {
  EngineApi,
  EngineAsset,
  EngineKind,
  EngineStatus,
  EngineWritePayload,
} from "../api/types";
import {
  apiForKind,
  kindLabel,
  slugify,
  statusLabel,
  SUPPORTED_KIND_OPTIONS,
  toDateTimeLocal,
} from "../model";
import { InlineFeedback } from "./InlineFeedback";

interface EngineDrawerProps {
  engine: EngineAsset | null;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (payload: EngineWritePayload) => void;
}

interface FormValues {
  name: string;
  slug: string;
  kind: EngineKind;
  api: EngineApi;
  target: string;
  connectTimeout: string;
  requestTimeout: string;
  enabled: boolean;
  status: EngineStatus;
  image: string;
  description: string;
  statusBasis: string;
  statusObservedAt: string;
  source: string;
  details: string;
}

function initialValues(engine: EngineAsset | null): FormValues {
  if (!engine) {
    return {
      name: "",
      slug: "",
      kind: "asr",
      api: "ux_speech",
      target: "",
      connectTimeout: "10",
      requestTimeout: "",
      enabled: true,
      status: "observed_running",
      image: "",
      description: "",
      statusBasis: "",
      statusObservedAt: "",
      source: "",
      details: "{}",
    };
  }
  return {
    name: engine.name,
    slug: engine.slug,
    kind: engine.kind,
    api: engine.api,
    target: engine.target,
    connectTimeout: String(engine.connect_timeout_seconds),
    requestTimeout:
      engine.request_timeout_seconds === null
        ? ""
        : String(engine.request_timeout_seconds),
    enabled: engine.enabled,
    status: engine.status,
    image: engine.image ?? "",
    description: engine.description ?? "",
    statusBasis: engine.status_basis ?? "",
    statusObservedAt: toDateTimeLocal(engine.status_observed_at),
    source: engine.source ?? "",
    details: JSON.stringify(engine.details, null, 2),
  };
}

function optional(value: string): string | null {
  const normalized = value.trim();
  return normalized || null;
}

function FieldError({ children }: { children?: string }) {
  return children ? <span className="field-error">{children}</span> : null;
}

const STATUS_OPTIONS: EngineStatus[] = [
  "observed_running",
  "confirmed_running",
  "inactive",
];

function focusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [href], [tabindex]:not([tabindex="-1"])',
    ),
  );
}

export function EngineDrawer({
  engine,
  busy,
  error,
  onClose,
  onSubmit,
}: EngineDrawerProps) {
  const [values, setValues] = useState(() => initialValues(engine));
  const [slugTouched, setSlugTouched] = useState(Boolean(engine));
  const [validation, setValidation] = useState<Record<string, string>>({});
  const drawerRef = useRef<HTMLElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null,
  );

  useEffect(() => {
    const previousFocus = previousFocusRef.current;
    return () => previousFocus?.focus();
  }, []);

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape" && !busy) {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab" || !drawerRef.current) {
      return;
    }
    const focusable = focusableElements(drawerRef.current);
    if (focusable.length === 0) {
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const update = <Key extends keyof FormValues>(
    key: Key,
    value: FormValues[Key],
  ) => {
    setValues((current) => ({ ...current, [key]: value }));
    setValidation((current) => {
      if (!(key in current)) {
        return current;
      }
      const next = { ...current };
      delete next[key];
      return next;
    });
  };

  const handleName = (name: string) => {
    setValues((current) => ({
      ...current,
      name,
      slug: slugTouched ? current.slug : slugify(name),
    }));
  };

  const handleKind = (kind: EngineKind) => {
    setValues((current) => ({
      ...current,
      kind,
      api: apiForKind(kind) ?? current.api,
    }));
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    const errors: Record<string, string> = {};
    const name = values.name.trim();
    const slug = values.slug.trim();
    const target = values.target.trim();
    const connectTimeout = Number(values.connectTimeout);
    const requestTimeout = values.requestTimeout.trim()
      ? Number(values.requestTimeout)
      : null;

    if (!name) errors.name = "请输入引擎名称";
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug)) {
      errors.slug = "仅允许小写字母、数字和单个连字符";
    }
    if (!target) errors.target = "请输入 gRPC 地址";
    if (!Number.isFinite(connectTimeout) || connectTimeout <= 0) {
      errors.connectTimeout = "连接超时必须大于 0";
    }
    if (
      requestTimeout !== null &&
      (!Number.isFinite(requestTimeout) || requestTimeout <= 0)
    ) {
      errors.requestTimeout = "请求超时必须大于 0";
    }

    let details: Record<string, unknown> = {};
    try {
      const parsed: unknown = JSON.parse(values.details || "{}");
      if (
        typeof parsed !== "object" ||
        parsed === null ||
        Array.isArray(parsed)
      ) {
        errors.details = "details 必须是 JSON 对象";
      } else {
        details = parsed as Record<string, unknown>;
      }
    } catch {
      errors.details = "请输入合法的 JSON";
    }

    if (Object.keys(errors).length > 0) {
      setValidation(errors);
      return;
    }

    onSubmit({
      name,
      slug,
      kind: values.kind,
      api: values.api,
      protocol: "grpc",
      target,
      connect_timeout_seconds: connectTimeout,
      request_timeout_seconds: requestTimeout,
      enabled: values.enabled,
      status: values.status,
      image: optional(values.image),
      description: optional(values.description),
      status_basis: optional(values.statusBasis),
      status_observed_at: values.statusObservedAt
        ? new Date(values.statusObservedAt).toISOString()
        : null,
      source: optional(values.source),
      details,
    });
  };

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={onClose}>
      <aside
        className="engine-drawer"
        ref={drawerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
        onKeyDown={handleKeyDown}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="drawer-heading">
          <div>
            <h2 id="drawer-title">{engine ? "编辑引擎" : "新建引擎"}</h2>
            <p>
              {engine
                ? "更新资产信息和运行时连接参数。"
                : "注册一个可用于语音处理流程的引擎实例。"}
            </p>
          </div>
          <button
            className="drawer-close"
            type="button"
            onClick={onClose}
            aria-label="关闭编辑面板"
            disabled={busy}
          >
            ×
          </button>
        </div>

        <form className="engine-form" onSubmit={handleSubmit}>
          <div className="form-section">
            <div className="form-section-heading">
              <h3>基本信息</h3>
              <p>用于识别和检索该引擎资产。</p>
            </div>
            <div className="form-grid">
              <label className="field field-span-2">
                <span>名称</span>
                <input
                  autoFocus
                  value={values.name}
                  onChange={(event) => handleName(event.target.value)}
                  maxLength={200}
                  placeholder="例如：Qwen ASR 线上实例"
                />
                <FieldError>{validation.name}</FieldError>
              </label>
              <label className="field field-span-2">
                <span>Slug</span>
                <input
                  value={values.slug}
                  onChange={(event) => {
                    setSlugTouched(true);
                    update("slug", event.target.value);
                  }}
                  maxLength={100}
                  placeholder="qwen-asr-production"
                />
                <FieldError>{validation.slug}</FieldError>
              </label>
              <label className="field">
                <span>能力类型</span>
                <select
                  value={values.kind}
                  onChange={(event) =>
                    handleKind(event.target.value as EngineKind)
                  }
                >
                  {!SUPPORTED_KIND_OPTIONS.some(
                    (option) => option.value === values.kind,
                  ) && (
                    <option value={values.kind}>
                      {kindLabel(values.kind)}
                    </option>
                  )}
                  {SUPPORTED_KIND_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field field-span-2">
                <span>描述</span>
                <textarea
                  rows={3}
                  value={values.description}
                  onChange={(event) =>
                    update("description", event.target.value)
                  }
                  placeholder="说明该引擎适用的场景或用途"
                />
              </label>
            </div>
          </div>

          <div className="form-section">
            <div className="form-section-heading">
              <h3>连接信息</h3>
              <p>
                连接会在绑定时验证；已绑定资产修改运行参数时也会先验证再保存。
              </p>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>接口</span>
                <input value={values.api} readOnly aria-readonly="true" />
              </label>
              <label className="field">
                <span>协议</span>
                <input value="gRPC" readOnly aria-readonly="true" />
              </label>
              <label className="field field-span-2">
                <span>gRPC 地址</span>
                <input
                  value={values.target}
                  onChange={(event) => update("target", event.target.value)}
                  maxLength={500}
                  placeholder="127.0.0.1:50052"
                />
                <FieldError>{validation.target}</FieldError>
              </label>
              <label className="field">
                <span>连接超时（秒）</span>
                <input
                  type="number"
                  min="0.01"
                  step="0.01"
                  value={values.connectTimeout}
                  onChange={(event) =>
                    update("connectTimeout", event.target.value)
                  }
                />
                <FieldError>{validation.connectTimeout}</FieldError>
              </label>
              <label className="field">
                <span>请求超时（秒）</span>
                <input
                  type="number"
                  min="0.01"
                  step="0.01"
                  value={values.requestTimeout}
                  onChange={(event) =>
                    update("requestTimeout", event.target.value)
                  }
                  placeholder="不限制"
                />
                <FieldError>{validation.requestTimeout}</FieldError>
              </label>
            </div>
          </div>

          <div className="form-section">
            <div className="form-section-heading">
              <h3>状态记录</h3>
              <p>这是资产目录记录，不代表持续实时健康检查。</p>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>目录状态</span>
                <select
                  value={values.status}
                  onChange={(event) =>
                    update("status", event.target.value as EngineStatus)
                  }
                >
                  {!STATUS_OPTIONS.includes(values.status) && (
                    <option value={values.status}>
                      {statusLabel(values.status)}
                    </option>
                  )}
                  {STATUS_OPTIONS.map((status) => (
                    <option key={status} value={status}>
                      {statusLabel(status)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="switch-field">
                <input
                  type="checkbox"
                  checked={values.enabled}
                  onChange={(event) => update("enabled", event.target.checked)}
                />
                <span aria-hidden="true" className="switch-control" />
                <span>允许绑定和运行</span>
              </label>
              <label className="field field-span-2">
                <span>状态依据</span>
                <input
                  value={values.statusBasis}
                  onChange={(event) =>
                    update("statusBasis", event.target.value)
                  }
                />
              </label>
              <label className="field">
                <span>状态记录时间</span>
                <input
                  type="datetime-local"
                  value={values.statusObservedAt}
                  onChange={(event) =>
                    update("statusObservedAt", event.target.value)
                  }
                />
              </label>
            </div>
          </div>

          <details className="advanced-section">
            <summary>高级信息</summary>
            <div className="form-grid">
              <label className="field field-span-2">
                <span>镜像</span>
                <input
                  value={values.image}
                  onChange={(event) => update("image", event.target.value)}
                  maxLength={500}
                  placeholder="registry.example.com/asr:version"
                />
              </label>
              <label className="field">
                <span>来源</span>
                <input
                  value={values.source}
                  onChange={(event) => update("source", event.target.value)}
                  maxLength={500}
                />
              </label>
              <label className="field field-span-2">
                <span>Details（JSON 对象）</span>
                <textarea
                  className="code-input"
                  rows={7}
                  spellCheck={false}
                  value={values.details}
                  onChange={(event) => update("details", event.target.value)}
                />
                <FieldError>{validation.details}</FieldError>
              </label>
            </div>
          </details>

          {error && (
            <InlineFeedback title="引擎保存失败" message={error} />
          )}

          <div className="drawer-actions">
            <span>{engine ? `当前 revision ${engine.revision}` : "协议固定为 gRPC"}</span>
            <button
              className="button button-quiet"
              type="button"
              onClick={onClose}
              disabled={busy}
            >
              取消
            </button>
            <button
              className="button button-primary"
              type="submit"
              disabled={busy}
            >
              {busy ? "正在保存…" : engine ? "保存更改" : "创建引擎"}
            </button>
          </div>
        </form>
      </aside>
    </div>
  );
}
