import type { HealthStatus } from "../api/types";

interface StatusBarProps {
  health: HealthStatus | null;
  loading: boolean;
  onRefresh: () => void;
  onCreate?: () => void;
  subtitle?: string;
}

function HealthMark({
  label,
  ready,
}: {
  label: string;
  ready: boolean | null;
}) {
  return (
    <span className="health-mark">
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
  onCreate,
  subtitle = "引擎配置工作台",
}: StatusBarProps) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          AH
        </span>
        <div>
          <strong>Audio Harness</strong>
          <span>{subtitle}</span>
        </div>
      </div>

      <div className="topbar-actions">
        <div className="health-cluster" aria-label="服务健康状态">
          <HealthMark
            label="SQLite"
            ready={health ? health.database === "ready" : null}
          />
          <HealthMark
            label="数据面"
            ready={health ? health.data_plane === "ready" : null}
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
        {onCreate && (
          <button
            className="button button-primary"
            type="button"
            onClick={onCreate}
          >
            新建引擎
          </button>
        )}
      </div>
    </header>
  );
}
