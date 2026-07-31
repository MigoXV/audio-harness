import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import {
  errorMessage,
  reportRequestError,
} from "../api/client";
import type {
  AudioHarnessApi,
  EngineAsset,
  EngineBinding,
} from "../api/types";
import {
  capabilityLabel,
  engineState,
  formatDate,
  protocolLabel,
  roleLabel,
  type PipelineStageDefinition,
} from "../model";
import { InlineFeedback } from "./InlineFeedback";

interface EnginePickerDrawerProps {
  api: AudioHarnessApi;
  binding: EngineBinding;
  definition: PipelineStageDefinition | null;
  busy: boolean;
  error?: string;
  onApply: (engineId: string) => Promise<boolean>;
  onClose: () => void;
  onManageLibrary: () => void;
}

function focusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => !element.hasAttribute("hidden"));
}

export function EnginePickerDrawer({
  api,
  binding,
  definition,
  busy,
  error,
  onApply,
  onClose,
  onManageLibrary,
}: EnginePickerDrawerProps) {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<EngineAsset[]>([]);
  const [selectedId, setSelectedId] = useState(binding.engine?.id ?? "");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [catalogEmpty, setCatalogEmpty] = useState(false);
  const [reloadVersion, setReloadVersion] = useState(0);
  const drawerRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null,
  );

  const requiredApi = definition?.requiredApi ?? binding.engine?.api ?? null;
  const stageName = definition?.name ?? roleLabel(binding.role);
  const rawCapability = definition?.capability ?? binding.role;

  useEffect(() => {
    const previousFocus = previousFocusRef.current;
    searchRef.current?.focus();
    return () => previousFocus?.focus();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(async () => {
      setLoading(true);
      setLoadError(null);
      try {
        const [result, catalogProbe] = await Promise.all([
          api.listEngines(
            {
              limit: 200,
              enabled: true,
              api: requiredApi ?? undefined,
              q: query.trim() || undefined,
            },
            controller.signal,
          ),
          query.trim()
            ? Promise.resolve(null)
            : api.listEngines({ limit: 1 }, controller.signal),
        ]);
        if (controller.signal.aborted) {
          return;
        }
        if (catalogProbe !== null) {
          setCatalogEmpty(catalogProbe.total === 0);
        }
        const compatible = result.items.filter(
          (engine) =>
            engine.enabled &&
            (requiredApi === null || engine.api === requiredApi),
        );
        setCandidates(compatible);
      } catch (reason) {
        if (!controller.signal.aborted) {
          reportRequestError("读取候选引擎失败", reason);
          setCandidates([]);
          setLoadError(
            errorMessage(reason, "暂时无法获取候选引擎，请稍后重试。"),
          );
        }
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      }
    }, 250);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [api, query, reloadVersion, requiredApi]);

  const selected = useMemo(
    () => candidates.find((candidate) => candidate.id === selectedId) ?? null,
    [candidates, selectedId],
  );

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

  const apply = async () => {
    if (!selectedId || selectedId === binding.engine?.id || busy) {
      return;
    }
    if (await onApply(selectedId)) {
      onClose();
    }
  };

  return (
    <div
      className="drawer-backdrop"
      role="presentation"
      onMouseDown={() => !busy && onClose()}
    >
      <aside
        className="picker-drawer"
        ref={drawerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="picker-title"
        onKeyDown={handleKeyDown}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="drawer-heading">
          <div>
            <h2 id="picker-title">选择{stageName}引擎</h2>
            <p>
              当前阶段：{stageName} · {capabilityLabel(rawCapability)}
            </p>
          </div>
          <button
            className="drawer-close"
            type="button"
            aria-label="关闭候选引擎抽屉"
            disabled={busy}
            onClick={onClose}
          >
            ×
          </button>
        </header>

        <div className="picker-content">
          <label className="search-field picker-search">
            <span className="sr-only">搜索候选引擎</span>
            <input
              ref={searchRef}
              type="search"
              placeholder="搜索名称、标识或 Endpoint"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>

          {requiredApi === null && (
            <p className="picker-compatibility-note">
              当前接口未提供该能力的兼容约束，最终由服务端在绑定时验证。
            </p>
          )}

          {loadError && (
            <InlineFeedback
              title="候选引擎加载失败"
              message={loadError}
              actionLabel="重新加载"
              onAction={() => setReloadVersion((version) => version + 1)}
            />
          )}

          {loading && candidates.length === 0 && (
            <div className="picker-loading" aria-label="正在读取候选引擎">
              <span />
              <span />
              <span />
            </div>
          )}

          {!loading && !loadError && candidates.length === 0 && (
            <div className="picker-empty">
              <strong>
                {catalogEmpty && !query.trim()
                  ? "还没有注册引擎"
                  : query.trim()
                  ? "没有找到匹配的候选引擎"
                  : "没有可用的兼容引擎"}
              </strong>
              <p>
                {catalogEmpty && !query.trim()
                  ? "请先在引擎库中注册语音处理服务。"
                  : query.trim()
                  ? "请尝试其他名称、标识或 Endpoint。"
                  : `引擎库中暂时没有已启用且支持 ${capabilityLabel(
                      rawCapability,
                    )} 的引擎。`}
              </p>
              <button
                className="text-button"
                type="button"
                onClick={onManageLibrary}
              >
                前往引擎库
              </button>
            </div>
          )}

          {candidates.length > 0 && (
            <div className="candidate-list" role="radiogroup">
              {candidates.map((candidate) => {
                const state = engineState(candidate);
                return (
                  <label
                    className={`candidate-item ${
                      selectedId === candidate.id ? "is-selected" : ""
                    }`}
                    key={candidate.id}
                  >
                    <input
                      type="radio"
                      name="engine-candidate"
                      value={candidate.id}
                      checked={selectedId === candidate.id}
                      disabled={busy}
                      onChange={() => setSelectedId(candidate.id)}
                    />
                    <span className="candidate-copy">
                      <strong>{candidate.name}</strong>
                      <code>{candidate.target}</code>
                      <span>
                        {protocolLabel(candidate.protocol)} · {state.label} ·
                        更新于 {formatDate(candidate.updated_at)}
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
          )}

          {error && (
            <InlineFeedback title="验证和应用失败" message={error} />
          )}
        </div>

        <footer className="picker-actions">
          <p>
            系统将验证连接；成功后立即替换当前绑定并用于后续会话，失败时保留原绑定。
          </p>
          {selected && (
            <span>
              已选择 <strong>{selected.name}</strong>
            </span>
          )}
          <div>
            <button
              className="button button-quiet"
              type="button"
              disabled={busy}
              onClick={onClose}
            >
              取消
            </button>
            <button
              className="button button-primary"
              type="button"
              disabled={
                busy ||
                !selectedId ||
                selectedId === binding.engine?.id ||
                loading
              }
              onClick={() => void apply()}
            >
              {busy
                ? "正在验证并应用…"
                : binding.engine
                  ? "确认更换"
                  : "确认绑定"}
            </button>
          </div>
        </footer>
      </aside>
    </div>
  );
}
