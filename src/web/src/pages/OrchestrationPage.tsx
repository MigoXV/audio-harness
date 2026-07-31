import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate } from "react-router-dom";

import {
  ApiError,
  errorMessage,
  reportRequestError,
} from "../api/client";
import type {
  AudioHarnessApi,
  EngineAsset,
  HealthStatus,
  NodeTypeDefinition,
  PipelineDefinition,
  PipelineDiagnostic,
  PipelineDraft,
  PipelineRevision,
} from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { DagEditor } from "../components/DagEditor";
import { EnginePickerDrawer } from "../components/EnginePickerDrawer";
import { InlineFeedback } from "../components/InlineFeedback";
import { NodeConfigDrawer } from "../components/NodeConfigDrawer";
import { PipelineView } from "../components/PipelineView";
import { nodeDisplayName } from "../model";

interface OrchestrationPageProps {
  api: AudioHarnessApi;
  health: HealthStatus | null;
  healthLoading: boolean;
  healthError: string | null;
  refreshVersion: number;
  onRefreshHealth: () => Promise<void>;
}

interface SelectedNode {
  nodeId: string;
  nodeType: NodeTypeDefinition;
}

interface PipelineSummary {
  tone: "success" | "warning" | "danger" | "muted";
  title: string;
  message: string;
  details: string[];
}

function sameDefinition(
  left: PipelineDefinition | null,
  right: PipelineDefinition | null,
): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function workCopyMatchesActive(
  draft: PipelineDefinition | null,
  active: PipelineRevision | null,
  engines: Map<string, EngineAsset>,
): boolean {
  if (!draft || !active || !sameDefinition(draft, active.definition)) {
    return false;
  }
  return draft.nodes.every((node) => {
    if (!node.engine_id) {
      return active.engine_revisions[node.id] === undefined;
    }
    return (
      engines.get(node.engine_id)?.revision ===
      active.engine_revisions[node.id]
    );
  });
}

function summarizePipeline({
  draft,
  active,
  localDirty,
  loading,
  loadError,
  health,
  healthLoading,
  healthError,
  workCopyActive,
}: {
  draft: PipelineDraft | null;
  active: PipelineRevision | null;
  localDirty: boolean;
  loading: boolean;
  loadError: string | null;
  health: HealthStatus | null;
  healthLoading: boolean;
  healthError: string | null;
  workCopyActive: boolean;
}): PipelineSummary {
  if (loadError && draft === null) {
    return {
      tone: "danger",
      title: "处理链加载失败",
      message: "暂时无法读取当前工作副本。",
      details: [],
    };
  }
  if (loading && draft === null) {
    return {
      tone: "muted",
      title: "正在读取处理链",
      message: "正在加载工作副本、节点类型和引擎目录。",
      details: [],
    };
  }
  if (draft === null) {
    return {
      tone: "danger",
      title: "处理链不可用",
      message: "当前没有可读取的处理链工作副本。",
      details: [],
    };
  }
  if (localDirty) {
    return {
      tone: "warning",
      title: "存在未保存修改",
      message: "先保存工作副本，再校验并激活新的处理链修订。",
      details: [],
    };
  }
  if (active === null) {
    return {
      tone: "danger",
      title: "尚未激活处理链",
      message: "当前工作副本不会处理新会话，请完成校验并激活。",
      details: [],
    };
  }
  if (!workCopyActive) {
    return {
      tone: "warning",
      title: "工作副本尚未生效",
      message: `当前运行修订为 ${active.revision}，工作副本需要重新校验并激活。`,
      details: [],
    };
  }
  if (healthError) {
    return {
      tone: "warning",
      title: "配置已激活，运行状态未知",
      message: "暂时无法确认数据面和各节点的运行状态。",
      details: [],
    };
  }
  if (healthLoading || health === null) {
    return {
      tone: "muted",
      title: "正在确认运行状态",
      message: `处理链修订 ${active.revision} 已激活，正在检查数据面。`,
      details: [],
    };
  }
  if (health.data_plane !== "ready") {
    return {
      tone: "danger",
      title: "数据面未就绪",
      message: "当前处理链暂时不能接收新的音频会话。",
      details: health.problems,
    };
  }
  return {
    tone: "success",
    title: "当前处理链可用",
    message: `修订 ${active.revision} 已激活，数据面和节点已经就绪。`,
    details: [],
  };
}

function diagnosticsFromError(reason: unknown): PipelineDiagnostic[] {
  if (reason instanceof ApiError && Array.isArray(reason.details)) {
    return reason.details.filter(
      (item): item is PipelineDiagnostic =>
        item !== null &&
        typeof item === "object" &&
        typeof (item as PipelineDiagnostic).code === "string" &&
        typeof (item as PipelineDiagnostic).message === "string",
    );
  }
  return [];
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
  const [draft, setDraft] = useState<PipelineDraft | null>(null);
  const [active, setActive] = useState<PipelineRevision | null>(null);
  const [definition, setDefinition] =
    useState<PipelineDefinition | null>(null);
  const [nodeTypes, setNodeTypes] = useState<NodeTypeDefinition[]>([]);
  const [engines, setEngines] = useState<EngineAsset[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [picker, setPicker] = useState<SelectedNode | null>(null);
  const [configNodeId, setConfigNodeId] = useState<string | null>(null);
  const [removeNodeId, setRemoveNodeId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [activating, setActivating] = useState(false);
  const [valid, setValid] = useState(false);
  const [diagnostics, setDiagnostics] = useState<PipelineDiagnostic[]>([]);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const loadSequence = useRef(0);
  const editSequence = useRef(0);
  const validationSequence = useRef(0);
  const saveInFlight = useRef(false);
  const activationInFlight = useRef(false);

  const loadPage = useCallback(
    async (signal?: AbortSignal) => {
      const requestId = ++loadSequence.current;
      setLoading(true);
      setLoadError(null);
      try {
        const [nextDraft, nextTypes, engineResult, activeResult] =
          await Promise.all([
            api.getPipeline(signal),
            api.listNodeTypes(signal),
            api.listEngines({ limit: 200 }, signal),
            api.getActivePipeline(signal).catch((reason) => {
              if (
                reason instanceof ApiError &&
                reason.code === "pipeline_not_active"
              ) {
                return null;
              }
              throw reason;
            }),
          ]);
        if (
          signal?.aborted ||
          requestId !== loadSequence.current
        ) {
          return;
        }
        const listedIds = new Set(
          engineResult.items.map((engine) => engine.id),
        );
        const referencedIds = new Set(
          [
            ...nextDraft.definition.nodes,
            ...(activeResult?.definition.nodes ?? []),
          ]
            .map((node) => node.engine_id)
            .filter((id): id is string => id !== null),
        );
        const missingResults = await Promise.allSettled(
          Array.from(referencedIds)
            .filter((id) => !listedIds.has(id))
            .map((id) => api.getEngine(id, signal)),
        );
        if (
          signal?.aborted ||
          requestId !== loadSequence.current
        ) {
          return;
        }
        const referencedEngines = missingResults.flatMap((result) =>
          result.status === "fulfilled" ? [result.value] : [],
        );
        editSequence.current += 1;
        validationSequence.current += 1;
        setDraft(nextDraft);
        setDefinition(nextDraft.definition);
        setNodeTypes(nextTypes);
        setEngines([...engineResult.items, ...referencedEngines]);
        setActive(activeResult);
        setValidating(false);
        setValid(false);
        setDiagnostics([]);
        setOperationError(null);
      } catch (reason) {
        if (
          !signal?.aborted &&
          requestId === loadSequence.current
        ) {
          reportRequestError("读取 DAG 编排工作台失败", reason);
          setLoadError(
            errorMessage(reason, "处理链加载失败，请稍后重试。"),
          );
        }
      } finally {
        if (
          !signal?.aborted &&
          requestId === loadSequence.current
        ) {
          setLoading(false);
        }
      }
    },
    [api],
  );

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => {
      void loadPage(controller.signal);
    }, 0);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [loadPage, refreshVersion]);

  const enginesById = useMemo(
    () => new Map(engines.map((engine) => [engine.id, engine])),
    [engines],
  );
  const nodeTypesByName = useMemo(
    () => new Map(nodeTypes.map((type) => [type.type, type])),
    [nodeTypes],
  );
  const localDirty = Boolean(
    draft &&
      definition &&
      !sameDefinition(draft.definition, definition),
  );
  const draftChanged = Boolean(
    draft &&
      active &&
      !workCopyMatchesActive(
        draft.definition,
        active,
        enginesById,
      ),
  );
  const saved = Boolean(draft && definition && !localDirty);
  const workCopyActive = workCopyMatchesActive(
    draft?.definition ?? null,
    active,
    enginesById,
  );
  const summary = summarizePipeline({
    draft,
    active,
    localDirty,
    loading,
    loadError,
    health,
    healthLoading,
    healthError,
    workCopyActive,
  });

  const updateDefinition = (next: PipelineDefinition) => {
    editSequence.current += 1;
    validationSequence.current += 1;
    setValidating(false);
    setDefinition(next);
    setValid(false);
    setDiagnostics([]);
    setOperationError(null);
    setNotice(null);
  };

  const save = async () => {
    if (!draft || !definition || saveInFlight.current) {
      return;
    }
    const savedEditSequence = editSequence.current;
    saveInFlight.current = true;
    setSaving(true);
    setOperationError(null);
    setNotice(null);
    try {
      const next = await api.updatePipelineDraft(
        definition,
        draft.revision,
      );
      setDraft(next);
      if (editSequence.current === savedEditSequence) {
        setDefinition(next.definition);
      }
      validationSequence.current += 1;
      setValid(false);
      setDiagnostics([]);
      setNotice(
        editSequence.current === savedEditSequence
          ? "工作副本已保存，下一步请校验处理链。"
          : "先前版本已保存；保存期间产生的新修改仍留在本地。",
      );
    } catch (reason) {
      reportRequestError("保存处理链工作副本失败", reason);
      setOperationError(
        errorMessage(reason, "工作副本保存失败，请稍后重试。"),
      );
    } finally {
      saveInFlight.current = false;
      setSaving(false);
    }
  };

  const validate = async () => {
    if (!saved || validating) {
      return;
    }
    const requestId = ++validationSequence.current;
    setValidating(true);
    setOperationError(null);
    setNotice(null);
    try {
      const result = await api.validatePipeline();
      if (requestId !== validationSequence.current) {
        return;
      }
      setValid(result.valid);
      setDiagnostics(result.diagnostics);
      setNotice(
        result.valid
          ? "处理链结构、节点配置和引擎兼容性校验通过。"
          : null,
      );
    } catch (reason) {
      if (requestId !== validationSequence.current) {
        return;
      }
      reportRequestError("校验处理链失败", reason);
      setValid(false);
      setDiagnostics(diagnosticsFromError(reason));
      setOperationError(
        errorMessage(reason, "处理链校验失败，请稍后重试。"),
      );
    } finally {
      if (requestId === validationSequence.current) {
        setValidating(false);
      }
    }
  };

  const activate = async () => {
    if (
      !draft ||
      !saved ||
      !valid ||
      activationInFlight.current
    ) {
      return;
    }
    activationInFlight.current = true;
    setActivating(true);
    setOperationError(null);
    setNotice(null);
    try {
      const revision = await api.activatePipeline(draft.revision);
      setActive(revision);
      setDraft({ ...draft, active_revision: revision.revision });
      setNotice(
        `处理链修订 ${revision.revision} 已激活，后续会话将使用新执行计划。`,
      );
      await onRefreshHealth();
    } catch (reason) {
      reportRequestError("激活处理链失败", reason);
      const serverDiagnostics = diagnosticsFromError(reason);
      if (serverDiagnostics.length > 0) {
        setDiagnostics(serverDiagnostics);
      }
      setOperationError(
        errorMessage(reason, "处理链激活失败，当前运行修订保持不变。"),
      );
    } finally {
      activationInFlight.current = false;
      setActivating(false);
    }
  };

  const refreshPage = async () => {
    await Promise.allSettled([loadPage(), onRefreshHealth()]);
  };

  const selectedPickerNode = picker
    ? definition?.nodes.find((node) => node.id === picker.nodeId) ?? null
    : null;
  const configNode = configNodeId
    ? definition?.nodes.find((node) => node.id === configNodeId) ?? null
    : null;
  const configNodeType = configNode
    ? nodeTypesByName.get(configNode.type) ?? null
    : null;
  const removeNode = removeNodeId
    ? definition?.nodes.find((node) => node.id === removeNodeId) ?? null
    : null;

  const chooseEngine = async (engineId: string): Promise<boolean> => {
    if (!definition || !selectedPickerNode) {
      return false;
    }
    updateDefinition({
      ...definition,
      nodes: definition.nodes.map((node) =>
        node.id === selectedPickerNode.id
          ? { ...node, engine_id: engineId }
          : node,
      ),
    });
    return true;
  };

  return (
    <main className={`orchestration-page ${mode === "edit" ? "is-editing" : ""}`}>
      {mode === "view" && (
        <>
          <header className="orchestration-heading">
            <div>
              <h1>默认语音处理链</h1>
              <p>
                查看音频从输入到会话输出的执行关系，并维护节点使用的引擎。
              </p>
            </div>
            <div className="orchestration-heading-actions">
              {active && <span>运行修订 {active.revision}</span>}
              <button
                className="button button-secondary"
                type="button"
                disabled={!definition || loading}
                onClick={() => setMode("edit")}
              >
                编辑拓扑
              </button>
              <button
                className="button button-quiet"
                type="button"
                disabled={loading || healthLoading}
                onClick={() => void refreshPage()}
              >
                {loading || healthLoading ? "刷新中…" : "刷新状态"}
              </button>
            </div>
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
            {(loadError || healthError) && (
              <button
                className="text-button"
                type="button"
                onClick={() => void refreshPage()}
              >
                重新加载
              </button>
            )}
          </section>
        </>
      )}

      {operationError && (
        <InlineFeedback
          title="操作未完成"
          message={operationError}
          actionLabel="关闭"
          onAction={() => setOperationError(null)}
        />
      )}
      {notice && (
        <div className="settings-feedback settings-success" role="status">
          {notice}
        </div>
      )}

      {definition && nodeTypes.length > 0 ? (
        mode === "edit" ? (
          <DagEditor
            definition={definition}
            nodeTypes={nodeTypes}
            engines={enginesById}
            diagnostics={diagnostics}
            saving={saving}
            validating={validating}
            activating={activating}
            saved={saved}
            valid={valid}
            activationNeeded={!workCopyActive}
            onChange={updateDefinition}
            onConfigureNode={(node) => setConfigNodeId(node.id)}
            onSave={() => void save()}
            onValidate={() => void validate()}
            onActivate={() => void activate()}
            onClose={() => setMode("view")}
          />
        ) : (
          <PipelineView
            definition={definition}
            engines={enginesById}
            nodeTypes={nodeTypes}
            health={health}
            draftChanged={localDirty || draftChanged}
            onPickEngine={(node, nodeType) =>
              setPicker({ nodeId: node.id, nodeType })
            }
            onRemoveNode={(node) => setRemoveNodeId(node.id)}
          />
        )
      ) : loading ? (
        <div className="pipeline-skeleton" aria-label="正在读取处理链">
          <span />
          <span />
          <span />
        </div>
      ) : (
        <div className="quiet-state">
          <strong>还没有可配置的处理阶段</strong>
          <p>当前配置中尚未定义语音处理节点。</p>
        </div>
      )}

      {mode === "view" && definition && (
        <footer className="pipeline-workcopy-actions">
          <span>
            {localDirty
              ? "当前有未保存的本地修改"
              : draftChanged
                ? "工作副本与运行修订不同"
                : "工作副本与运行修订一致"}
          </span>
          <div>
            <button
              className="button button-quiet"
              type="button"
              disabled={!localDirty || saving}
              onClick={() => {
                if (draft) {
                  setDefinition(draft.definition);
                  setValid(false);
                  setDiagnostics([]);
                }
              }}
            >
              放弃本地修改
            </button>
            <button
              className="button button-secondary"
              type="button"
              disabled={!localDirty || saving}
              onClick={() => void save()}
            >
              {saving ? "保存中…" : "保存工作副本"}
            </button>
            <button
              className="button button-secondary"
              type="button"
              disabled={!saved || validating}
              onClick={() => void validate()}
            >
              {validating ? "校验中…" : "校验处理链"}
            </button>
            <button
              className="button button-primary"
              type="button"
              disabled={
                !saved ||
                !valid ||
                workCopyActive ||
                activating
              }
              onClick={() => void activate()}
            >
              {activating ? "激活中…" : "激活修订"}
            </button>
          </div>
        </footer>
      )}

      {picker && selectedPickerNode && (
        <EnginePickerDrawer
          key={picker.nodeId}
          api={api}
          node={selectedPickerNode}
          nodeType={picker.nodeType}
          currentEngine={
            selectedPickerNode.engine_id
              ? enginesById.get(selectedPickerNode.engine_id) ?? null
              : null
          }
          busy={false}
          onApply={chooseEngine}
          onClose={() => setPicker(null)}
          onManageLibrary={() => {
            setPicker(null);
            navigate("/engines");
          }}
        />
      )}

      {configNode && configNodeType && (
        <NodeConfigDrawer
          key={configNode.id}
          node={configNode}
          nodeType={configNodeType}
          engine={
            configNode.engine_id
              ? enginesById.get(configNode.engine_id) ?? null
              : null
          }
          onChange={(nextNode) => {
            if (!definition) {
              return;
            }
            updateDefinition({
              ...definition,
              nodes: definition.nodes.map((node) =>
                node.id === nextNode.id ? nextNode : node,
              ),
            });
          }}
          onPickEngine={() => {
            setPicker({ nodeId: configNode.id, nodeType: configNodeType });
            setConfigNodeId(null);
          }}
          onClose={() => setConfigNodeId(null)}
        />
      )}

      <ConfirmDialog
        open={removeNode !== null}
        title={`移除“${
          removeNode
            ? nodeDisplayName(removeNode, nodeTypes)
            : ""
        }”节点`}
        description="节点及与它相连的边会从本地工作副本中移除；保存和激活前不会影响当前运行修订。"
        confirmLabel="确认移除"
        busy={false}
        onCancel={() => setRemoveNodeId(null)}
        onConfirm={() => {
          if (definition && removeNode) {
            updateDefinition({
              ...definition,
              nodes: definition.nodes.filter(
                (node) => node.id !== removeNode.id,
              ),
              edges: definition.edges.filter(
                (edge) =>
                  edge.source_node !== removeNode.id &&
                  edge.target_node !== removeNode.id,
              ),
            });
          }
          setRemoveNodeId(null);
        }}
      />
    </main>
  );
}
