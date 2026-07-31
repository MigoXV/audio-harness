import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  errorMessage,
  reportRequestError,
} from "../api/client";
import type {
  AudioHarnessApi,
  EngineBinding,
  EngineRole,
  HealthStatus,
} from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EnginePickerDrawer } from "../components/EnginePickerDrawer";
import { InlineFeedback } from "../components/InlineFeedback";
import { PipelineView } from "../components/PipelineView";
import {
  engineState,
  PIPELINE_STAGES,
  roleLabel,
  type PipelineStageDefinition,
} from "../model";

interface OrchestrationPageProps {
  api: AudioHarnessApi;
  health: HealthStatus | null;
  healthLoading: boolean;
  healthError: string | null;
  refreshVersion: number;
  onRefreshHealth: () => Promise<void>;
}

interface SelectedStage {
  binding: EngineBinding;
  definition: PipelineStageDefinition | null;
}

interface PipelineSummary {
  tone: "success" | "warning" | "danger" | "muted";
  title: string;
  message: string;
  details: string[];
}

function summarizePipeline(
  bindings: EngineBinding[],
  bindingsLoading: boolean,
  bindingsError: string | null,
  health: HealthStatus | null,
  healthLoading: boolean,
  healthError: string | null,
): PipelineSummary {
  if (bindingsError && bindings.length === 0) {
    return {
      tone: "danger",
      title: "处理链加载失败",
      message: "暂时无法读取当前运行配置。",
      details: [],
    };
  }
  if (bindingsLoading && bindings.length === 0) {
    return {
      tone: "muted",
      title: "正在读取处理链",
      message: "正在加载当前默认角色及其引擎绑定。",
      details: [],
    };
  }
  if (bindings.length === 0) {
    return {
      tone: "muted",
      title: "还没有可配置的处理阶段",
      message: "当前配置中尚未定义语音处理角色。",
      details: [],
    };
  }

  const byRole = new Map(bindings.map((binding) => [binding.role, binding]));
  const missing = PIPELINE_STAGES.filter(
    (stage) => stage.required && !byRole.get(stage.role)?.engine,
  );
  if (missing.length > 0) {
    return {
      tone: "danger",
      title: "当前配置暂不可用",
      message: `请先配置${missing.map((stage) => stage.name).join("、")}。`,
      details: [],
    };
  }

  const invalid = PIPELINE_STAGES.flatMap((stage) => {
    const engine = byRole.get(stage.role)?.engine;
    if (!engine) {
      return [];
    }
    if (engine.api !== stage.requiredApi) {
      return [`${stage.name}的引擎接口与能力要求不一致`];
    }
    if (!engine.enabled || engine.status === "inactive") {
      return [`${stage.name}当前绑定的引擎已停用`];
    }
    return [];
  });
  if (invalid.length > 0) {
    return {
      tone: "danger",
      title: "当前配置存在异常",
      message: "请处理异常节点后再接收新的音频请求。",
      details: invalid,
    };
  }

  if (healthError) {
    return {
      tone: "warning",
      title: "配置已读取，运行状态未知",
      message: "暂时无法确认管理服务和数据面连接状态。",
      details: [],
    };
  }
  if (healthLoading || health === null) {
    return {
      tone: "muted",
      title: "正在确认处理链状态",
      message: "配置已经读取，正在检查数据面连接。",
      details: [],
    };
  }
  if (health.data_plane !== "ready") {
    return {
      tone: "danger",
      title: "数据面未就绪",
      message: "当前处理链暂时不能接收新的音频请求。",
      details: health.problems,
    };
  }

  const pending = bindings.filter(
    (binding) =>
      binding.engine &&
      engineState(binding.engine).tone === "pending",
  );
  return {
    tone: pending.length > 0 ? "warning" : "success",
    title: "当前配置可用",
    message:
      pending.length > 0
        ? `数据面已连接；${pending.length} 个引擎的目录状态仍待确认。`
        : "必需阶段已经配置，数据面已连接。",
    details: [],
  };
}

export function OrchestrationPage({
  api,
  health,
  healthLoading,
  healthError,
  refreshVersion,
  onRefreshHealth,
}: OrchestrationPageProps) {
  const navigate = useNavigate();
  const [bindings, setBindings] = useState<EngineBinding[]>([]);
  const [loading, setLoading] = useState(true);
  const [bindingsError, setBindingsError] = useState<string | null>(null);
  const [selectedStage, setSelectedStage] =
    useState<SelectedStage | null>(null);
  const [pendingRole, setPendingRole] = useState<EngineRole | null>(null);
  const [roleErrors, setRoleErrors] = useState<
    Record<string, string | undefined>
  >({});
  const [roleNotices, setRoleNotices] = useState<
    Record<string, string | undefined>
  >({});
  const [unbindTarget, setUnbindTarget] =
    useState<EngineBinding | null>(null);
  const [unbindBusy, setUnbindBusy] = useState(false);

  const refreshBindings = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setBindingsError(null);
      try {
        const nextBindings = await api.listBindings(signal);
        if (!signal?.aborted) {
          setBindings(nextBindings);
        }
      } catch (reason) {
        if (!signal?.aborted) {
          reportRequestError("读取处理链绑定失败", reason);
          setBindingsError(
            errorMessage(reason, "处理链加载失败，请稍后重试。"),
          );
        }
      } finally {
        if (!signal?.aborted) {
          setLoading(false);
        }
      }
    },
    [api],
  );

  useEffect(() => {
    const controller = new AbortController();
    void refreshBindings(controller.signal);
    return () => controller.abort();
  }, [refreshBindings, refreshVersion]);

  const summary = useMemo(
    () =>
      summarizePipeline(
        bindings,
        loading,
        bindingsError,
        health,
        healthLoading,
        healthError,
      ),
    [
      bindings,
      bindingsError,
      health,
      healthError,
      healthLoading,
      loading,
    ],
  );

  const refreshPage = async () => {
    await Promise.allSettled([
      refreshBindings(),
      onRefreshHealth(),
    ]);
  };

  const bind = async (engineId: string): Promise<boolean> => {
    if (!selectedStage || pendingRole !== null) {
      return false;
    }
    const role = selectedStage.binding.role;
    setPendingRole(role);
    setRoleErrors((current) => ({ ...current, [role]: undefined }));
    setRoleNotices((current) => ({ ...current, [role]: undefined }));
    try {
      const updated = await api.bind(role, engineId);
      setBindings((current) =>
        current.some((binding) => binding.role === role)
          ? current.map((binding) =>
              binding.role === role ? updated : binding,
            )
          : [...current, updated],
      );
      setRoleNotices((current) => ({
        ...current,
        [role]:
          "连接验证成功，新的绑定已经立即生效；后续会话将使用该引擎。",
      }));
      await Promise.allSettled([
        refreshBindings(),
        onRefreshHealth(),
      ]);
      return true;
    } catch (reason) {
      reportRequestError(`${roleLabel(role)}切换失败`, reason);
      setRoleErrors((current) => ({
        ...current,
        [role]: errorMessage(
          reason,
          "连接验证或切换未完成，原绑定保持不变。",
        ),
      }));
      return false;
    } finally {
      setPendingRole(null);
    }
  };

  const unbind = async () => {
    if (!unbindTarget) {
      return;
    }
    const role = unbindTarget.role;
    setUnbindBusy(true);
    try {
      await api.unbind(role);
      setBindings((current) =>
        current.map((binding) =>
          binding.role === role
            ? {
                ...binding,
                engine: null,
                updated_at: null,
              }
            : binding,
        ),
      );
      setRoleErrors((current) => ({ ...current, [role]: undefined }));
      setRoleNotices((current) => ({
        ...current,
        [role]: "该阶段已经解除绑定。",
      }));
      setUnbindTarget(null);
      await Promise.allSettled([
        refreshBindings(),
        onRefreshHealth(),
      ]);
    } catch (reason) {
      reportRequestError(`${roleLabel(role)}解除绑定失败`, reason);
      setRoleErrors((current) => ({
        ...current,
        [role]: errorMessage(reason, "解除绑定失败，请稍后重试。"),
      }));
      setUnbindTarget(null);
    } finally {
      setUnbindBusy(false);
    }
  };

  const unbindDefinition = unbindTarget
    ? PIPELINE_STAGES.find((stage) => stage.role === unbindTarget.role)
    : null;

  return (
    <main className="orchestration-page">
      <header className="orchestration-heading">
        <div>
          <h1>默认语音处理链</h1>
          <p>配置音频输入经过的处理阶段，以及每个阶段实际使用的引擎。</p>
        </div>
        <button
          className="button button-quiet"
          type="button"
          disabled={loading || healthLoading}
          onClick={() => void refreshPage()}
        >
          {loading || healthLoading ? "刷新中…" : "刷新状态"}
        </button>
      </header>

      <section
        className={`pipeline-summary pipeline-summary-${summary.tone}`}
        aria-live="polite"
      >
        <span className="summary-mark" aria-hidden="true" />
        <div>
          <strong>{summary.title}</strong>
          <p>{summary.message}</p>
          {summary.details.length > 0 && (
            <ul>
              {summary.details.map((detail) => (
                <li key={detail}>{detail}</li>
              ))}
            </ul>
          )}
        </div>
        {(bindingsError || healthError) && (
          <button
            className="text-button"
            type="button"
            onClick={() => void refreshPage()}
          >
            重新加载
          </button>
        )}
      </section>

      {bindingsError && bindings.length > 0 && (
        <InlineFeedback
          title="当前处理链可能不是最新状态"
          message={bindingsError}
          actionLabel="重新加载"
          onAction={() => void refreshBindings()}
        />
      )}

      {loading && bindings.length === 0 ? (
        <div className="pipeline-skeleton" aria-label="正在读取处理链">
          <span />
          <span />
          <span />
        </div>
      ) : (
        <PipelineView
          bindings={bindings}
          pendingRole={pendingRole}
          roleErrors={roleErrors}
          roleNotices={roleNotices}
          onPick={(binding, definition) => {
            setRoleErrors((current) => ({
              ...current,
              [binding.role]: undefined,
            }));
            setSelectedStage({ binding, definition });
          }}
          onUnbind={setUnbindTarget}
        />
      )}

      {selectedStage && (
        <EnginePickerDrawer
          key={selectedStage.binding.role}
          api={api}
          binding={selectedStage.binding}
          definition={selectedStage.definition}
          busy={pendingRole === selectedStage.binding.role}
          error={roleErrors[selectedStage.binding.role]}
          onApply={bind}
          onClose={() => {
            if (pendingRole === null) {
              setSelectedStage(null);
            }
          }}
          onManageLibrary={() => {
            setSelectedStage(null);
            navigate("/engines");
          }}
        />
      )}

      <ConfirmDialog
        open={unbindTarget !== null}
        title={`解除${roleLabel(unbindTarget?.role ?? "")}绑定`}
        description={
          unbindDefinition?.required
            ? "解除必需阶段后，数据面会进入未就绪状态，新的音频流将无法开始。"
            : "解除后，新会话将不再使用这一可选处理阶段。"
        }
        confirmLabel="确认解除"
        busy={unbindBusy}
        onCancel={() => !unbindBusy && setUnbindTarget(null)}
        onConfirm={() => void unbind()}
      />
    </main>
  );
}
