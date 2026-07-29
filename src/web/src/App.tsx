import { useCallback, useEffect, useMemo, useState } from "react";

import { apiClient, errorMessage } from "./api/client";
import type {
  AudioHarnessApi,
  EngineAsset,
  EngineBinding,
  EngineRole,
  EngineWritePayload,
  HealthStatus,
} from "./api/types";
import { BindingPanel } from "./components/BindingPanel";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { EngineDrawer } from "./components/EngineDrawer";
import {
  EngineList,
  type EngineListFilters,
} from "./components/EngineList";
import { StatusBar } from "./components/StatusBar";
import { ROLE_LABELS } from "./model";

interface AppProps {
  api?: AudioHarnessApi;
}

type Confirmation =
  | { type: "delete"; engine: EngineAsset }
  | { type: "unbind"; binding: EngineBinding };

const PAGE_SIZE = 50;

export function App({ api = apiClient }: AppProps) {
  const [engines, setEngines] = useState<EngineAsset[]>([]);
  const [total, setTotal] = useState(0);
  const [filters, setFilters] = useState<EngineListFilters>({
    query: "",
    kind: "",
    enabled: "",
  });
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [bindings, setBindings] = useState<EngineBinding[]>([]);
  const [candidates, setCandidates] = useState<EngineAsset[]>([]);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [supportLoading, setSupportLoading] = useState(true);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [selectedEngine, setSelectedEngine] = useState<EngineAsset | null>(null);
  const [drawer, setDrawer] = useState<"create" | "edit" | null>(null);
  const [drawerEngine, setDrawerEngine] = useState<EngineAsset | null>(null);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [pendingRole, setPendingRole] = useState<EngineRole | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
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

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(async () => {
      setListLoading(true);
      setListError(null);
      try {
        const result = await api.listEngines(listParams, controller.signal);
        setEngines(result.items);
        setTotal(result.total);
      } catch (error) {
        if (!controller.signal.aborted) {
          setListError(errorMessage(error));
        }
      } finally {
        if (!controller.signal.aborted) {
          setListLoading(false);
        }
      }
    }, 250);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [api, listParams, refreshVersion]);

  const refreshHealth = useCallback(
    async (signal?: AbortSignal) => {
      try {
        setHealth(await api.getReadiness(signal));
      } catch (error) {
        if (!signal?.aborted) {
          setNotice({ tone: "error", message: errorMessage(error) });
        }
      }
    },
    [api],
  );

  const refreshSupportData = useCallback(
    async (signal?: AbortSignal) => {
      setSupportLoading(true);
      try {
        const [nextBindings, asr, vad, nextHealth] = await Promise.all([
          api.listBindings(signal),
          api.listEngines(
            { limit: 200, api: "ux_speech", enabled: true },
            signal,
          ),
          api.listEngines(
            { limit: 200, api: "ux_vad", enabled: true },
            signal,
          ),
          api.getReadiness(signal),
        ]);
        setBindings(nextBindings);
        setCandidates([...asr.items, ...vad.items]);
        setHealth(nextHealth);
      } catch (error) {
        if (!signal?.aborted) {
          setNotice({ tone: "error", message: errorMessage(error) });
        }
      } finally {
        if (!signal?.aborted) {
          setSupportLoading(false);
        }
      }
    },
    [api],
  );

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => {
      void refreshSupportData(controller.signal);
    }, 0);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [refreshSupportData, refreshVersion]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void refreshHealth();
      }
    }, 10_000);
    return () => window.clearInterval(interval);
  }, [refreshHealth]);

  const refreshAll = () => {
    setNotice(null);
    setRefreshVersion((version) => version + 1);
  };

  const loadMore = async () => {
    if (listLoading || engines.length >= total) {
      return;
    }
    setListLoading(true);
    setListError(null);
    try {
      const result = await api.listEngines({
        ...listParams,
        offset: engines.length,
      });
      setEngines((current) => [...current, ...result.items]);
      setTotal(result.total);
    } catch (error) {
      setListError(errorMessage(error));
    } finally {
      setListLoading(false);
    }
  };

  const openCreate = () => {
    setDrawerEngine(null);
    setFormError(null);
    setDrawer("create");
  };

  const openEdit = (engine: EngineAsset) => {
    setSelectedEngine(engine);
    setDrawerEngine(engine);
    setFormError(null);
    setDrawer("edit");
  };

  const closeDrawer = () => {
    if (!saving) {
      setDrawer(null);
      setFormError(null);
    }
  };

  const saveEngine = async (payload: EngineWritePayload) => {
    setSaving(true);
    setFormError(null);
    try {
      const saved =
        drawer === "edit" && drawerEngine
          ? await api.updateEngine(drawerEngine.id, payload)
          : await api.createEngine(payload);
      setSelectedEngine(saved);
      setDrawer(null);
      setNotice({
        tone: "success",
        message: drawer === "edit" ? "引擎资产已更新" : "引擎资产已创建",
      });
      setRefreshVersion((version) => version + 1);
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  const bind = async (role: EngineRole, engineId: string) => {
    setPendingRole(role);
    setNotice(null);
    try {
      await api.bind(role, engineId);
      setNotice({
        tone: "success",
        message: `${ROLE_LABELS[role]}已完成探活并切换`,
      });
      setRefreshVersion((version) => version + 1);
    } catch (error) {
      setNotice({
        tone: "error",
        message: `${ROLE_LABELS[role]}切换失败：${errorMessage(error)}`,
      });
      await refreshSupportData();
    } finally {
      setPendingRole(null);
    }
  };

  const confirmAction = async () => {
    if (!confirmation) {
      return;
    }
    setConfirmBusy(true);
    try {
      if (confirmation.type === "delete") {
        await api.deleteEngine(confirmation.engine.id);
        if (selectedEngine?.id === confirmation.engine.id) {
          setSelectedEngine(null);
        }
        setNotice({ tone: "success", message: "引擎资产已删除" });
      } else {
        await api.unbind(confirmation.binding.role);
        setNotice({
          tone: "success",
          message: `${ROLE_LABELS[confirmation.binding.role]}已解除绑定`,
        });
      }
      setConfirmation(null);
      setRefreshVersion((version) => version + 1);
    } catch (error) {
      setNotice({ tone: "error", message: errorMessage(error) });
      setConfirmation(null);
    } finally {
      setConfirmBusy(false);
    }
  };

  const confirmationCopy = confirmation
    ? confirmation.type === "delete"
      ? {
          title: `删除“${confirmation.engine.name}”`,
          description:
            "删除后无法从控制面恢复；已绑定的引擎会被服务端拒绝删除。",
          label: "确认删除",
        }
      : {
          title: `解除${ROLE_LABELS[confirmation.binding.role]}绑定`,
          description:
            confirmation.binding.role === "semantic_vad"
              ? "新会话将回退到物理 VAD 的分段结果。"
              : "解除必需角色后，数据面会进入 NOT READY，新的音频流将无法开始。",
          label: "确认解除",
        }
    : null;

  return (
    <div className="app-shell">
      <StatusBar
        health={health}
        loading={listLoading || supportLoading}
        onRefresh={refreshAll}
        onCreate={openCreate}
      />

      {notice && (
        <div className={`notice notice-${notice.tone}`} role="status">
          <span>{notice.message}</span>
          <button
            type="button"
            aria-label="关闭提示"
            onClick={() => setNotice(null)}
          >
            ×
          </button>
        </div>
      )}

      <main className="workbench">
        <EngineList
          engines={engines}
          total={total}
          filters={filters}
          loading={listLoading}
          error={listError}
          selectedId={selectedEngine?.id ?? null}
          onFiltersChange={setFilters}
          onSelect={setSelectedEngine}
          onEdit={openEdit}
          onDelete={(engine) => setConfirmation({ type: "delete", engine })}
          onLoadMore={() => void loadMore()}
        />
        <BindingPanel
          bindings={bindings}
          candidates={candidates}
          health={health}
          loading={supportLoading}
          pendingRole={pendingRole}
          selectedEngine={selectedEngine}
          onBind={(role, engineId) => void bind(role, engineId)}
          onUnbind={(binding) =>
            setConfirmation({ type: "unbind", binding })
          }
        />
      </main>

      {drawer && (
        <EngineDrawer
          key={drawerEngine?.id ?? "new"}
          engine={drawerEngine}
          busy={saving}
          error={formError}
          onClose={closeDrawer}
          onSubmit={(payload) => void saveEngine(payload)}
        />
      )}

      <ConfirmDialog
        open={Boolean(confirmation)}
        title={confirmationCopy?.title ?? ""}
        description={confirmationCopy?.description ?? ""}
        confirmLabel={confirmationCopy?.label ?? ""}
        busy={confirmBusy}
        onCancel={() => !confirmBusy && setConfirmation(null)}
        onConfirm={() => void confirmAction()}
      />
    </div>
  );
}
