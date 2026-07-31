import { useCallback, useEffect, useState } from "react";
import { Link, Navigate, Route, Routes } from "react-router-dom";

import {
  apiClient,
  errorMessage,
  reportRequestError,
} from "./api/client";
import type {
  AudioHarnessApi,
  HealthStatus,
} from "./api/types";
import { RuntimeSettingsPanel } from "./components/RuntimeSettingsPanel";
import { StatusBar } from "./components/StatusBar";
import { EngineLibraryPage } from "./pages/EngineLibraryPage";
import { OrchestrationPage } from "./pages/OrchestrationPage";

interface AppProps {
  api?: AudioHarnessApi;
}

function NotFoundPage() {
  return (
    <main className="not-found-page">
      <strong>页面不存在</strong>
      <p>当前地址不属于 Audio Harness 控制面。</p>
      <Link className="button button-primary" to="/orchestration">
        返回编排工作台
      </Link>
    </main>
  );
}

export function App({ api = apiClient }: AppProps) {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthLoading, setHealthLoading] = useState(true);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);

  const refreshHealth = useCallback(async () => {
    setHealthLoading(true);
    setHealthError(null);
    try {
      setHealth(await api.getReadiness());
    } catch (reason) {
      reportRequestError("读取系统状态失败", reason);
      setHealth(null);
      setHealthError(
        errorMessage(reason, "暂时无法确认管理服务和数据面状态。"),
      );
    } finally {
      setHealthLoading(false);
    }
  }, [api]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      void refreshHealth();
    }, 0);
    return () => window.clearTimeout(timeout);
  }, [refreshHealth, refreshVersion]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void refreshHealth();
      }
    }, 10_000);
    return () => window.clearInterval(interval);
  }, [refreshHealth]);

  const refreshAll = () => {
    setRefreshVersion((version) => version + 1);
  };

  return (
    <div className="app-shell">
      <StatusBar
        health={health}
        loading={healthLoading}
        onRefresh={refreshAll}
      />

      <Routes>
        <Route path="/" element={<Navigate replace to="/orchestration" />} />
        <Route
          path="/orchestration"
          element={
            <OrchestrationPage
              api={api}
              health={health}
              healthLoading={healthLoading}
              healthError={healthError}
              refreshVersion={refreshVersion}
              onRefreshHealth={refreshHealth}
            />
          }
        />
        <Route
          path="/engines"
          element={
            <EngineLibraryPage
              api={api}
              refreshVersion={refreshVersion}
            />
          }
        />
        <Route
          path="/settings"
          element={
            <RuntimeSettingsPanel
              api={api}
              refreshVersion={refreshVersion}
            />
          }
        />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </div>
  );
}
