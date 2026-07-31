import { useEffect, useMemo, useState } from "react";

import {
  ApiError,
  errorMessage,
  reportRequestError,
} from "../api/client";
import type {
  AudioHarnessApi,
  EngineAsset,
  EngineWritePayload,
  PipelineDefinition,
} from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EngineDrawer } from "../components/EngineDrawer";
import {
  EngineList,
  type EngineListFilters,
} from "../components/EngineList";
import { InlineFeedback } from "../components/InlineFeedback";
import { nodeDisplayName } from "../model";

interface EngineLibraryPageProps {
  api: AudioHarnessApi;
  refreshVersion: number;
}

const PAGE_SIZE = 50;

export function EngineLibraryPage({
  api,
  refreshVersion,
}: EngineLibraryPageProps) {
  const [engines, setEngines] = useState<EngineAsset[]>([]);
  const [total, setTotal] = useState(0);
  const [filters, setFilters] = useState<EngineListFilters>({
    query: "",
    kind: "",
    enabled: "",
  });
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [pipelineReferences, setPipelineReferences] = useState<
    Array<{ label: string; definition: PipelineDefinition }>
  >([]);
  const [pipelineError, setPipelineError] = useState<string | null>(null);
  const [localRefresh, setLocalRefresh] = useState(0);
  const [drawer, setDrawer] = useState<"create" | "edit" | null>(null);
  const [drawerEngine, setDrawerEngine] = useState<EngineAsset | null>(null);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<EngineAsset | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [notice, setNotice] = useState<{
    tone: "success" | "error";
    message: string;
  } | null>(null);

  const listParams = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: 0,
      q: filters.query.trim() || undefined,
      kind: filters.kind || undefined,
      enabled:
        filters.enabled === "" ? undefined : filters.enabled === "true",
    }),
    [filters.enabled, filters.kind, filters.query],
  );

  const usageByEngine = useMemo(() => {
    if (pipelineError) {
      return null;
    }
    const usage = new Map<string, string[]>();
    for (const reference of pipelineReferences) {
      for (const node of reference.definition.nodes) {
        if (!node.engine_id) {
          continue;
        }
        const nodes = usage.get(node.engine_id) ?? [];
        nodes.push(`${reference.label} · ${nodeDisplayName(node)}`);
        usage.set(node.engine_id, nodes);
      }
    }
    return usage;
  }, [pipelineError, pipelineReferences]);

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(async () => {
      setLoading(true);
      setListError(null);
      try {
        const result = await api.listEngines(listParams, controller.signal);
        if (controller.signal.aborted) {
          return;
        }
        setEngines(result.items);
        setTotal(result.total);
        setCapabilities((current) =>
          Array.from(
            new Set([
              ...current,
              ...result.items.map((engine) => engine.kind),
            ]),
          ).sort((left, right) => left.localeCompare(right)),
        );
      } catch (reason) {
        if (!controller.signal.aborted) {
          reportRequestError("读取引擎库失败", reason);
          setListError(
            errorMessage(reason, "引擎库加载失败，请稍后重试。"),
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
  }, [api, listParams, localRefresh, refreshVersion]);

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => {
      setPipelineError(null);
      void Promise.all([
        api.getPipeline(controller.signal),
        api.getActivePipeline(controller.signal).catch((reason) => {
          if (
            reason instanceof ApiError &&
            reason.code === "pipeline_not_active"
          ) {
            return null;
          }
          throw reason;
        }),
      ])
        .then(([draft, active]) => {
          setPipelineReferences([
            { label: "工作副本", definition: draft.definition },
            ...(active
              ? [{ label: `运行修订 ${active.revision}`, definition: active.definition }]
              : []),
          ]);
        })
        .catch((reason) => {
          if (!controller.signal.aborted) {
            reportRequestError("读取引擎使用情况失败", reason);
            setPipelineError(
              errorMessage(reason, "暂时无法读取处理链引用关系。"),
            );
          }
        });
    }, 0);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [api, localRefresh, refreshVersion]);

  const refresh = () => {
    setNotice(null);
    setLocalRefresh((version) => version + 1);
  };

  const loadMore = async () => {
    if (loading || engines.length >= total) {
      return;
    }
    setLoading(true);
    setListError(null);
    try {
      const result = await api.listEngines({
        ...listParams,
        offset: engines.length,
      });
      setEngines((current) => [...current, ...result.items]);
      setTotal(result.total);
      setCapabilities((current) =>
        Array.from(
          new Set([
            ...current,
            ...result.items.map((engine) => engine.kind),
          ]),
        ).sort((left, right) => left.localeCompare(right)),
      );
    } catch (reason) {
      reportRequestError("继续加载引擎库失败", reason);
      setListError(
        errorMessage(reason, "后续引擎加载失败，请稍后重试。"),
      );
    } finally {
      setLoading(false);
    }
  };

  const openCreate = () => {
    setDrawerEngine(null);
    setFormError(null);
    setDrawer("create");
  };

  const openEdit = (engine: EngineAsset) => {
    setDrawerEngine(engine);
    setFormError(null);
    setDrawer("edit");
  };

  const saveEngine = async (payload: EngineWritePayload) => {
    setSaving(true);
    setFormError(null);
    try {
      await (drawer === "edit" && drawerEngine
        ? api.updateEngine(drawerEngine.id, payload)
        : api.createEngine(payload));
      setDrawer(null);
      setNotice({
        tone: "success",
        message: drawer === "edit" ? "引擎已更新" : "引擎已注册",
      });
      setLocalRefresh((version) => version + 1);
    } catch (reason) {
      reportRequestError("保存引擎失败", reason);
      setFormError(
        errorMessage(reason, "引擎保存失败，请检查后重试。"),
      );
    } finally {
      setSaving(false);
    }
  };

  const deleteEngine = async () => {
    if (!deleteTarget) {
      return;
    }
    setDeleteBusy(true);
    try {
      await api.deleteEngine(deleteTarget.id);
      setDeleteTarget(null);
      setNotice({ tone: "success", message: "引擎已删除" });
      setLocalRefresh((version) => version + 1);
    } catch (reason) {
      reportRequestError("删除引擎失败", reason);
      setDeleteTarget(null);
      setNotice({
        tone: "error",
        message: errorMessage(reason, "引擎删除失败，请稍后重试。"),
      });
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <main className="engine-library-page">
      {notice && (
        <div className="page-notice">
          <InlineFeedback
            title={notice.tone === "success" ? "操作已完成" : "操作失败"}
            message={notice.message}
            tone={notice.tone}
            actionLabel="关闭"
            onAction={() => setNotice(null)}
          />
        </div>
      )}

      {pipelineError && (
        <div className="page-notice">
          <InlineFeedback
            title="使用情况暂不可用"
            message={pipelineError}
            actionLabel="重新加载"
            onAction={refresh}
          />
        </div>
      )}

      <EngineList
        engines={engines}
        total={total}
        capabilities={capabilities}
        usageByEngine={usageByEngine}
        filters={filters}
        loading={loading}
        error={listError}
        onFiltersChange={setFilters}
        onEdit={openEdit}
        onDelete={setDeleteTarget}
        onLoadMore={() => void loadMore()}
        onRetry={refresh}
        onCreate={openCreate}
      />

      {drawer && (
        <EngineDrawer
          key={drawerEngine?.id ?? "new"}
          engine={drawerEngine}
          busy={saving}
          error={formError}
          onClose={() => {
            if (!saving) {
              setDrawer(null);
              setFormError(null);
            }
          }}
          onSubmit={(payload) => void saveEngine(payload)}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title={`删除“${deleteTarget?.name ?? ""}”`}
        description="删除后将无法继续用于新的运行配置。如果该引擎仍被绑定，系统会阻止本次操作。"
        confirmLabel="确认删除"
        busy={deleteBusy}
        onCancel={() => !deleteBusy && setDeleteTarget(null)}
        onConfirm={() => void deleteEngine()}
      />
    </main>
  );
}
