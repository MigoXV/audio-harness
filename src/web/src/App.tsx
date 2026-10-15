import { lazy, Suspense, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { ShellNavigation } from "./components/ShellNavigation";

import { apiClient } from "./api/client";
import type { AudioHarnessApi } from "./api/types";
import { useRuntimeOverview } from "./features/health/useRuntimeOverview";

const EngineConsolePage = lazy(() => import("./pages/EngineConsolePage").then((module) => ({ default: module.EngineConsolePage })));
const AgentBackendPage = lazy(() => import("./pages/AgentBackendPage").then((module) => ({ default: module.AgentBackendPage })));
const RuntimePage = lazy(() => import("./pages/RuntimePage").then((module) => ({ default: module.RuntimePage })));
const WorkflowDetailPage = lazy(() => import("./pages/WorkflowDetailPage").then((module) => ({ default: module.WorkflowDetailPage })));
const WorkflowListPage = lazy(() => import("./pages/WorkflowListPage").then((module) => ({ default: module.WorkflowListPage })));
const VoiceAgentPage = lazy(() => import("./pages/VoiceAgentPage").then((module) => ({ default: module.VoiceAgentPage })));
const VoiceAgentDetailPage = lazy(() => import("./pages/VoiceAgentDetailPage").then((module) => ({ default: module.VoiceAgentDetailPage })));
const VoiceAgentCreatePage = lazy(() => import("./pages/VoiceAgentCreatePage").then((module) => ({ default: module.VoiceAgentCreatePage })));
const VectorWorkspacePage = lazy(() => import("./pages/VectorWorkspacePage").then((module) => ({ default: module.VectorWorkspacePage })));

interface AppProps { api?: AudioHarnessApi; }

export function App({ api = apiClient }: AppProps) {
  const [refreshVersion, setRefreshVersion] = useState(0);
  const overview = useRuntimeOverview(api);
  const refreshAll = () => { void overview.refetch(); setRefreshVersion((value) => value + 1); };

  return <div className="app-shell-new">
    <ShellNavigation overview={overview.data ?? null} failed={overview.isError} onRefresh={refreshAll} />
    <div className="app-content"><Suspense fallback={<main className="workspace-page"><div className="empty-state" role="status" aria-live="polite">正在加载页面…</div></main>}><Routes>
      <Route path="/" element={<Navigate replace to="/audio-workflows" />} />
      <Route path="/audio-workflows" element={<WorkflowListPage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/audio-workflows/:workflowId" element={<Navigate replace to="build" />} />
      <Route path="/audio-workflows/:workflowId/:tab" element={<WorkflowDetailPage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/voice-agents" element={<VoiceAgentPage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/voice-agents/new" element={<VoiceAgentCreatePage api={api} />} />
      <Route path="/voice-agents/:profileId" element={<Navigate replace to="build" />} />
      <Route path="/voice-agents/:profileId/:tab" element={<VoiceAgentDetailPage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/engines" element={<EngineConsolePage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/agent-backends" element={<AgentBackendPage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/vectors" element={<VectorWorkspacePage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/runtime" element={<RuntimePage api={api} refreshVersion={refreshVersion} />} />
      <Route path="/workflows" element={<Navigate replace to="/audio-workflows" />} />
      <Route path="/orchestration" element={<Navigate replace to="/audio-workflows" />} />
      <Route path="/settings" element={<Navigate replace to="/runtime" />} />
      <Route path="*" element={<main className="workspace-page"><div className="empty-state"><strong>页面不存在</strong><NavLink to="/audio-workflows">返回工作流</NavLink></div></main>} />
    </Routes></Suspense></div>
  </div>;
}
