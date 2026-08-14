import { useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { apiClient } from "./api/client";
import type { AudioHarnessApi, RuntimeOverview } from "./api/types";
import { useRuntimeOverview } from "./features/health/useRuntimeOverview";
import { EngineConsolePage } from "./pages/EngineConsolePage";
import { RuntimePage } from "./pages/RuntimePage";
import { WorkflowDetailPage } from "./pages/WorkflowDetailPage";
import { WorkflowListPage } from "./pages/WorkflowListPage";
import { VoiceAgentPage } from "./pages/VoiceAgentPage";
import { VoiceAgentDetailPage } from "./pages/VoiceAgentDetailPage";
import { VoiceAgentWizardPage } from "./pages/VoiceAgentWizardPage";
import { VectorWorkspacePage } from "./pages/VectorWorkspacePage";

interface AppProps { api?: AudioHarnessApi; }

function statusText(overview: RuntimeOverview | null, failed: boolean): string {
  if (failed) return "管理服务不可用";
  if (!overview) return "正在连接";
  if (overview.status === "ready") return "服务在线";
  if (overview.status === "unavailable") return "管理服务不可用";
  return `服务在线 · ${overview.issues.length} 项待配置`;
}

function ShellNavigation({ overview, failed, onRefresh }: { overview: RuntimeOverview | null; failed: boolean; onRefresh: () => void }) {
  const state = failed || overview?.status === "unavailable" ? "error" : overview?.status === "ready" ? "healthy" : "warning";
  return <>
    <header className="topbar">
      <NavLink className="brand" to="/audio-workflows" aria-label="Audio Harness 首页">
        <img className="brand-mark" src="/audio-harness-logo.svg" alt="" />
        <strong aria-hidden="true">Audio Harness</strong>
      </NavLink>
      <nav className="primary-navigation" aria-label="主导航"><NavLink to="/audio-workflows">语音工作流</NavLink><NavLink to="/voice-agents">语音智能体</NavLink><NavLink to="/vectors">向量检索</NavLink><NavLink to="/engines">推理引擎</NavLink><NavLink to="/runtime">接入与运行</NavLink></nav>
      <div className="topbar-meta"><span className={`health-dot ${state}`} /><NavLink className="status-link" to="/runtime#access">{statusText(overview, failed)}</NavLink><button className="text-button" onClick={onRefresh}>刷新</button></div>
    </header>
  </>;
}

export function App({ api = apiClient }: AppProps) {
  const [refreshVersion, setRefreshVersion] = useState(0);
  const overview = useRuntimeOverview(api);
  const refreshAll = () => { void overview.refetch(); setRefreshVersion((value) => value + 1); };

  return <div className="app-shell-new">
    <ShellNavigation overview={overview.data ?? null} failed={overview.isError} onRefresh={refreshAll} />
    <div className="app-content"><Routes>
      <Route path="/" element={<Navigate replace to="/audio-workflows" />} />
      <Route path="/audio-workflows" element={<WorkflowListPage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/audio-workflows/:workflowId" element={<Navigate replace to="build" />} />
      <Route path="/audio-workflows/:workflowId/:tab" element={<WorkflowDetailPage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/voice-agents" element={<VoiceAgentPage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/voice-agents/new" element={<VoiceAgentWizardPage api={api} />} />
      <Route path="/voice-agents/:profileId" element={<Navigate replace to="configure" />} />
      <Route path="/voice-agents/:profileId/:tab" element={<VoiceAgentDetailPage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/engines" element={<EngineConsolePage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/vectors" element={<VectorWorkspacePage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/runtime" element={<RuntimePage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/workflows" element={<Navigate replace to="/audio-workflows" />} />
      <Route path="/orchestration" element={<Navigate replace to="/audio-workflows" />} />
      <Route path="/settings" element={<Navigate replace to="/runtime" />} />
      <Route path="*" element={<main className="workspace-page"><div className="empty-state"><strong>页面不存在</strong><NavLink to="/audio-workflows">返回工作流</NavLink></div></main>} />
    </Routes></div>
  </div>;
}
