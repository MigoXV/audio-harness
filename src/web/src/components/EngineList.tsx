import type { EngineAsset } from "../api/types";
import { engineState, formatDate, kindLabel, roleLabel } from "../model";
import { ActionMenu } from "./ActionMenu";
import { InlineFeedback } from "./InlineFeedback";

export interface EngineListFilters {
  query: string;
  kind: string;
  enabled: "" | "true" | "false";
}

interface EngineListProps {
  engines: EngineAsset[];
  total: number;
  capabilities: string[];
  usageByEngine: Map<string, string[]> | null;
  filters: EngineListFilters;
  loading: boolean;
  error: string | null;
  onFiltersChange: (filters: EngineListFilters) => void;
  onEdit: (engine: EngineAsset) => void;
  onDelete: (engine: EngineAsset) => void;
  onLoadMore: () => void;
  onRetry: () => void;
  onCreate: () => void;
}

function StateText({ engine }: { engine: EngineAsset }) {
  const state = engineState(engine);
  return (
    <span className={`state-text state-${state.tone}`}>
      <span aria-hidden="true" />
      {state.label}
    </span>
  );
}

function EngineRow({
  engine,
  usage,
  onEdit,
  onDelete,
}: {
  engine: EngineAsset;
  usage: string;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <div className="engine-table-row" role="row">
      <div className="engine-cell engine-identity" role="cell">
        <strong>{engine.name}</strong>
        <span>
          <code>{engine.slug}</code>
          <span aria-hidden="true"> · </span>
          Revision {engine.revision}
        </span>
      </div>
      <div className="engine-cell capability-cell" role="cell">
        {kindLabel(engine.kind)}
      </div>
      <div className="engine-cell endpoint-cell" role="cell">
        <code title={engine.target}>{engine.target}</code>
        <span>{engine.protocol === "grpc" ? "gRPC" : engine.protocol}</span>
      </div>
      <div className="engine-cell status-cell" role="cell">
        <StateText engine={engine} />
      </div>
      <div className="engine-cell usage-cell" role="cell" title={usage}>
        {usage}
      </div>
      <time
        className="engine-cell time-cell"
        role="cell"
        dateTime={engine.updated_at}
      >
        {formatDate(engine.updated_at)}
      </time>
      <div
        className="engine-cell engine-actions"
        role="cell"
      >
        <button className="text-button" type="button" onClick={onEdit}>
          编辑
        </button>
        <ActionMenu
          items={[
            {
              label: "删除",
              tone: "danger",
              onSelect: onDelete,
            },
          ]}
        />
      </div>
    </div>
  );
}

function LoadingRows() {
  return (
    <div aria-label="正在读取引擎资产">
      {Array.from({ length: 5 }, (_, index) => (
        <div className="engine-table-row skeleton-row" role="row" key={index}>
          {Array.from({ length: 7 }, (__, cellIndex) => (
            <span role="cell" key={cellIndex} />
          ))}
        </div>
      ))}
    </div>
  );
}

export function EngineList({
  engines,
  total,
  capabilities,
  usageByEngine,
  filters,
  loading,
  error,
  onFiltersChange,
  onEdit,
  onDelete,
  onLoadMore,
  onRetry,
  onCreate,
}: EngineListProps) {
  const isFiltered = Boolean(
    filters.query.trim() || filters.kind || filters.enabled,
  );
  const showCollection =
    engines.length > 0 || loading || isFiltered || Boolean(error);
  const clearFilters = () =>
    onFiltersChange({ query: "", kind: "", enabled: "" });

  return (
    <section className="engine-workspace" aria-labelledby="engine-list-title">
      <header className="engine-page-heading">
        <div>
          <h1 id="engine-list-title">引擎库</h1>
          <p>管理可用于语音处理流程的引擎实例、能力类型和连接状态。</p>
        </div>
        <div className="engine-heading-actions">
          <span>{total} 个引擎</span>
          <button
            className="button button-primary"
            type="button"
            onClick={onCreate}
          >
            新建引擎
          </button>
        </div>
      </header>

      {error && (
        <InlineFeedback
          title="引擎列表加载失败"
          message={
            engines.length > 0
              ? "暂时无法连接管理服务，当前内容可能不是最新状态。"
              : error
          }
          actionLabel="重新加载"
          onAction={onRetry}
        />
      )}

      {showCollection && (
        <div className="engine-toolbar" aria-label="引擎搜索与筛选">
          <label className="search-field">
            <span className="sr-only">搜索引擎</span>
            <input
              type="search"
              placeholder="搜索名称、标识或 Endpoint"
              value={filters.query}
              onChange={(event) =>
                onFiltersChange({ ...filters, query: event.target.value })
              }
            />
          </label>
          <label className="filter-field">
            <span>能力</span>
            <select
              aria-label="能力筛选"
              value={filters.kind}
              onChange={(event) =>
                onFiltersChange({ ...filters, kind: event.target.value })
              }
            >
              <option value="">全部</option>
              {capabilities.map((capability) => (
                <option key={capability} value={capability}>
                  {kindLabel(capability)}
                </option>
              ))}
            </select>
          </label>
          <label className="filter-field">
            <span>启用状态</span>
            <select
              aria-label="启用状态筛选"
              value={filters.enabled}
              onChange={(event) =>
                onFiltersChange({
                  ...filters,
                  enabled: event.target.value as EngineListFilters["enabled"],
                })
              }
            >
              <option value="">全部</option>
              <option value="true">已启用</option>
              <option value="false">已停用</option>
            </select>
          </label>
        </div>
      )}

      {showCollection && (
        <div className="engine-table" role="table" aria-busy={loading}>
          <div className="engine-table-head" role="row">
            <span role="columnheader">引擎</span>
            <span role="columnheader">能力</span>
            <span role="columnheader">Endpoint / 协议</span>
            <span role="columnheader">状态</span>
            <span role="columnheader">使用情况</span>
            <span role="columnheader">更新时间</span>
            <span role="columnheader">操作</span>
          </div>

          {loading && engines.length === 0 && <LoadingRows />}
          {engines.map((engine) => {
            const roles = usageByEngine?.get(engine.id);
            const usage =
              usageByEngine === null
                ? "—"
                : roles && roles.length > 0
                  ? roles.map(roleLabel).join("、")
                  : "未使用";
            return (
              <EngineRow
                key={engine.id}
                engine={engine}
                usage={usage}
                onEdit={() => onEdit(engine)}
                onDelete={() => onDelete(engine)}
              />
            );
          })}
        </div>
      )}

      {!loading && !error && engines.length === 0 && (
        <div className="engine-empty-state">
          <strong>{isFiltered ? "没有找到匹配的引擎" : "还没有引擎"}</strong>
          <p>
            {isFiltered
              ? "尝试修改名称、能力类型或状态筛选。"
              : "注册 ASR、VAD 或其他语音处理服务后，可以在这里统一管理并用于运行时绑定。"}
          </p>
          <button
            className="button button-primary"
            type="button"
            onClick={isFiltered ? clearFilters : onCreate}
          >
            {isFiltered ? "清除筛选" : "注册第一个引擎"}
          </button>
        </div>
      )}

      {engines.length < total && (
        <button
          className="load-more"
          type="button"
          onClick={onLoadMore}
          disabled={loading}
        >
          {loading ? "加载中…" : `继续加载（${engines.length}/${total}）`}
        </button>
      )}
    </section>
  );
}
