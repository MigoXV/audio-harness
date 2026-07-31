import type {
  EngineBinding,
  EngineRole,
} from "../api/types";
import {
  capabilityLabel,
  engineState,
  formatDate,
  PIPELINE_STAGES,
  protocolLabel,
  roleLabel,
  stageDefinition,
  type PipelineStageDefinition,
} from "../model";
import { ActionMenu } from "./ActionMenu";
import { InlineFeedback } from "./InlineFeedback";

interface PipelineViewProps {
  bindings: EngineBinding[];
  pendingRole: EngineRole | null;
  roleErrors: Record<string, string | undefined>;
  roleNotices: Record<string, string | undefined>;
  onPick: (
    binding: EngineBinding,
    definition: PipelineStageDefinition | null,
  ) => void;
  onUnbind: (binding: EngineBinding) => void;
}

function placeholderBinding(role: EngineRole): EngineBinding {
  return {
    role,
    engine: null,
    generation: 0,
    updated_at: null,
  };
}

function PipelineStage({
  index,
  binding,
  definition,
  pending,
  error,
  notice,
  onPick,
  onUnbind,
}: {
  index?: number;
  binding: EngineBinding;
  definition: PipelineStageDefinition | null;
  pending: boolean;
  error?: string;
  notice?: string;
  onPick: () => void;
  onUnbind: () => void;
}) {
  const engine = binding.engine;
  const incompatible =
    engine !== null &&
    definition !== null &&
    engine.api !== definition.requiredApi;
  const state = error
    ? { label: "验证失败", tone: "danger" as const }
    : engine === null
      ? { label: "尚未配置", tone: "muted" as const }
      : incompatible
        ? { label: "配置异常", tone: "danger" as const }
        : engineState(engine);
  const stageName = definition?.name ?? roleLabel(binding.role);
  const capability = definition?.capability ?? binding.role;

  return (
    <section className="pipeline-stage">
      <div className="stage-index" aria-hidden="true">
        {index ? String(index).padStart(2, "0") : "—"}
      </div>
      <div className="stage-body">
        <header className="stage-heading">
          <div>
            <h3>{stageName}</h3>
            <span>
              {capabilityLabel(capability)}
              {definition
                ? ` · ${definition.required ? "必需" : "可选"}`
                : " · 顺序未定义"}
            </span>
          </div>
          <span className={`stage-state stage-state-${state.tone}`}>
            <span aria-hidden="true" />
            {state.label}
          </span>
        </header>

        {engine ? (
          <div className="stage-binding">
            <strong>{engine.name}</strong>
            <div>
              <code>{engine.target}</code>
              <span aria-hidden="true"> · </span>
              <span>{protocolLabel(engine.protocol)}</span>
            </div>
            <time dateTime={binding.updated_at ?? undefined}>
              绑定更新于 {formatDate(binding.updated_at)}
            </time>
          </div>
        ) : (
          <div className="stage-empty">
            <strong>尚未配置{stageName}引擎</strong>
            <p>
              {definition?.required
                ? `选择一个已启用且支持 ${capabilityLabel(
                    capability,
                  )} 的引擎后，才能完成当前处理配置。`
                : "该阶段为可选能力，不影响基础转写处理链运行。"}
            </p>
          </div>
        )}

        {error && (
          <InlineFeedback title={`${stageName}未完成切换`} message={error} />
        )}
        {notice && (
          <InlineFeedback
            title={`${stageName}绑定已更新`}
            message={notice}
            tone="success"
          />
        )}

        <div className="stage-actions">
          <button
            className="text-button"
            type="button"
            disabled={pending}
            onClick={onPick}
          >
            {engine ? "更换引擎" : "选择引擎"}
          </button>
          {engine && (
            <ActionMenu
              items={[
                {
                  label: "解除绑定",
                  tone: "danger",
                  disabled: pending,
                  onSelect: onUnbind,
                },
              ]}
            />
          )}
        </div>
      </div>
    </section>
  );
}

export function PipelineView({
  bindings,
  pendingRole,
  roleErrors,
  roleNotices,
  onPick,
  onUnbind,
}: PipelineViewProps) {
  const byRole = new Map(bindings.map((binding) => [binding.role, binding]));
  const knownRoles = new Set(PIPELINE_STAGES.map((stage) => stage.role));
  const unknownBindings = bindings.filter(
    (binding) => !knownRoles.has(binding.role),
  );

  return (
    <>
      <div className="pipeline-flow" aria-label="默认语音处理链">
        <div className="pipeline-terminal pipeline-input">
          <span>输入</span>
          <strong>音频输入</strong>
        </div>
        <div className="pipeline-connector" aria-hidden="true">
          <span>↓</span>
        </div>

        {PIPELINE_STAGES.map((definition, stageIndex) => {
          const binding =
            byRole.get(definition.role) ??
            placeholderBinding(definition.role);
          return (
            <div className="pipeline-stage-group" key={definition.role}>
              <PipelineStage
                index={stageIndex + 1}
                binding={binding}
                definition={definition}
                pending={pendingRole === binding.role}
                error={roleErrors[binding.role]}
                notice={roleNotices[binding.role]}
                onPick={() => onPick(binding, definition)}
                onUnbind={() => onUnbind(binding)}
              />
              <div className="pipeline-connector" aria-hidden="true">
                <span>↓</span>
              </div>
            </div>
          );
        })}

        <div className="pipeline-terminal pipeline-output">
          <span>输出</span>
          <strong>转写文本</strong>
        </div>
      </div>

      {unknownBindings.length > 0 && (
        <section className="unsequenced-bindings">
          <header>
            <h2>其他能力绑定</h2>
            <p>
              接口未提供这些能力的执行顺序，因此不会将它们伪装进上方处理链。
            </p>
          </header>
          {unknownBindings.map((binding) => (
            <PipelineStage
              key={binding.role}
              binding={binding}
              definition={stageDefinition(binding)}
              pending={pendingRole === binding.role}
              error={roleErrors[binding.role]}
              notice={roleNotices[binding.role]}
              onPick={() => onPick(binding, null)}
              onUnbind={() => onUnbind(binding)}
            />
          ))}
        </section>
      )}
    </>
  );
}
