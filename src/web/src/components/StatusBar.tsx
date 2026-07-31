import { NavLink } from "react-router-dom";

import type { HealthStatus } from "../api/types";

interface StatusBarProps {
  health: HealthStatus | null;
  loading: boolean;
  onRefresh: () => void;
}

function HealthMark({
  label,
  ready,
  title,
}: {
  label: string;
  ready: boolean | null;
  title: string;
}) {
  return (
    <span className="health-mark" title={title}>
      <span
        aria-hidden="true"
        className={`health-dot ${
          ready === null ? "is-unknown" : ready ? "is-ready" : "is-not-ready"
        }`}
      />
      <span>{label}</span>
    </span>
  );
}

export function StatusBar({
  health,
  loading,
  onRefresh,
}: StatusBarProps) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          AH
        </span>
        <strong>Audio Harness</strong>
      </div>

      <nav className="primary-navigation" aria-label="控制面导航">
        <NavLink to="/orchestration">编排</NavLink>
        <NavLink to="/engines">引擎库</NavLink>
        <NavLink to="/settings">运行设置</NavLink>
      </nav>

      <div className="topbar-actions">
        <div className="health-cluster" aria-label="服务健康状态">
          <HealthMark
            label={
              health
                ? health.database === "ready"
                  ? "管理服务正常"
                  : "管理服务异常"
                : "管理服务状态未知"
            }
            ready={health ? health.database === "ready" : null}
            title={
              health
                ? `元数据存储：SQLite；状态：${health.database}`
                : "元数据存储：SQLite；当前无法确认状态"
            }
          />
          <HealthMark
            label={
              health
                ? health.data_plane === "ready"
                  ? "数据面已连接"
                  : "数据面未就绪"
                : "数据面状态未知"
            }
            ready={health ? health.data_plane === "ready" : null}
            title={
              health
                ? `数据面状态：${health.data_plane}`
                : "当前无法确认数据面状态"
            }
          />
        </div>
        <button
          className="button button-quiet"
          type="button"
          onClick={onRefresh}
          disabled={loading}
        >
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>
    </header>
  );
}
