import { useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { apiClient } from "./api/client";
import type { AudioHarnessApi, HealthStatus } from "./api/types";
import { useReadiness } from "./features/health/useReadiness";
import { EngineConsolePage } from "./pages/EngineConsolePage";
import { RuntimePage } from "./pages/RuntimePage";
import { WorkflowDetailPage } from "./pages/WorkflowDetailPage";
import { WorkflowListPage } from "./pages/WorkflowListPage";

interface AppProps { api?: AudioHarnessApi; }

function ShellNavigation({ health, onRefresh }: { health: HealthStatus | null; onRefresh: () => void }) {
  return <>
    <header className="topbar">
      <div><span className="brand-mark">AH</span><strong>Audio Harness</strong></div>
      <nav className="primary-navigation" aria-label="主导航"><NavLink to="/workflows">工作流</NavLink><NavLink to="/engines">推理引擎</NavLink><NavLink to="/runtime">运行状态</NavLink></nav>
      <div className="topbar-meta"><span className={`health-dot ${health?.status === "ready" ? "healthy" : "warning"}`} aria-label={health?.status === "ready" ? "控制面已就绪" : "控制面需关注"} /><span>{health?.status === "ready" ? "控制面已就绪" : "控制面需关注"}</span><button className="text-button" onClick={onRefresh}>刷新</button></div>
    </header>
  </>;
}

export function App({ api = apiClient }: AppProps) {
  const [refreshVersion, setRefreshVersion] = useState(0);
  const readiness = useReadiness(api);
  const refreshAll = () => { void readiness.refetch(); setRefreshVersion((value) => value + 1); };

  return <div className="app-shell-new">
    <ShellNavigation health={readiness.data ?? null} onRefresh={refreshAll} />
    <div className="app-content"><Routes>
      <Route path="/" element={<Navigate replace to="/workflows" />} />
      <Route path="/workflows" element={<WorkflowListPage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/workflows/:workflowId" element={<Navigate replace to="overview" />} />
      <Route path="/workflows/:workflowId/:tab" element={<WorkflowDetailPage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/engines" element={<EngineConsolePage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/runtime" element={<RuntimePage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/orchestration" element={<Navigate replace to="/workflows" />} />
      <Route path="/settings" element={<Navigate replace to="/runtime" />} />
      <Route path="*" element={<main className="workspace-page"><div className="empty-state"><strong>页面不存在</strong><NavLink to="/workflows">返回工作流</NavLink></div></main>} />
    </Routes></div>
  </div>;
}
