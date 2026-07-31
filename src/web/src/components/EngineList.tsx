import type { EngineAsset, EngineKind } from "../api/types";
import {
  formatDate,
  KIND_LABELS,
  STATUS_LABELS,
} from "../model";

export interface EngineListFilters {
  query: string;
  kind: "" | EngineKind;
  enabled: "" | "true" | "false";
}

interface EngineListProps {
  engines: EngineAsset[];
  total: number;
  filters: EngineListFilters;
  loading: boolean;
  error: string | null;
  selectedId: string | null;
  onFiltersChange: (filters: EngineListFilters) => void;
  onSelect: (engine: EngineAsset) => void;
  onEdit: (engine: EngineAsset) => void;
  onDelete: (engine: EngineAsset) => void;
  onLoadMore: () => void;
}

const KIND_OPTIONS: Array<{ value: "" | EngineKind; label: string }> = [
  { value: "", label: "全部" },
  { value: "asr", label: "ASR" },
  { value: "vad", label: "VAD" },
  { value: "asr_vad", label: "ASR + VAD" },
];

function EngineRow({
  engine,
  selected,
  onSelect,
  onEdit,
  onDelete,
}: {
  engine: EngineAsset;
  selected: boolean;
  onSelect: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <article className={`engine-row ${selected ? "is-selected" : ""}`}>
      <button className="engine-row-main" type="button" onClick={onSelect}>
        <span className="engine-title-line">
          <strong>{engine.name}</strong>
          <span className={`status-badge status-${engine.status}`}>
            {STATUS_LABELS[engine.status]}
          </span>
          {!engine.enabled && <span className="status-badge">未启用</span>}
        </span>
        <span className="engine-meta">
          <span>{KIND_LABELS[engine.kind]}</span>
          <span>{engine.api}</span>
          <code>{engine.target}</code>
        </span>
        {engine.description && (
          <span className="engine-description">{engine.description}</span>
        )}
      </button>
      <div className="engine-row-aside">
        <span className="revision">r{engine.revision}</span>
        <time dateTime={engine.updated_at}>{formatDate(engine.updated_at)}</time>
        <div className="row-actions">
          <button className="text-button" type="button" onClick={onEdit}>
            编辑
          </button>
          <button
            className="text-button text-button-danger"
            type="button"
            onClick={onDelete}
          >
            删除
          </button>
        </div>
      </div>
    </article>
  );
}

export function EngineList({
  engines,
  total,
  filters,
  loading,
  error,
  selectedId,
  onFiltersChange,
  onSelect,
  onEdit,
  onDelete,
  onLoadMore,
}: EngineListProps) {
  return (
    <section className="engine-workspace" aria-labelledby="engine-list-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">ENGINE ASSETS</p>
          <h1 id="engine-list-title">引擎资产</h1>
        </div>
        <span className="section-count">{total} 项</span>
      </div>

      <div className="filters">
        <label className="search-field">
          <span className="sr-only">搜索引擎</span>
          <input
            type="search"
            placeholder="搜索名称、slug 或 gRPC 地址"
            value={filters.query}
            onChange={(event) =>
              onFiltersChange({ ...filters, query: event.target.value })
            }
          />
        </label>
        <div className="kind-tabs" aria-label="引擎类型筛选">
          {KIND_OPTIONS.map((option) => (
            <button
              className={filters.kind === option.value ? "is-active" : ""}
              key={option.value || "all"}
              type="button"
              onClick={() =>
                onFiltersChange({ ...filters, kind: option.value })
              }
            >
              {option.label}
            </button>
          ))}
        </div>
        <label className="compact-select">
          <span>启用状态</span>
          <select
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
            <option value="false">未启用</option>
          </select>
        </label>
      </div>

      <div className="engine-stream" aria-busy={loading}>
        {error && <div className="inline-error">{error}</div>}
        {!error && loading && engines.length === 0 && (
          <div className="quiet-state">正在读取引擎资产…</div>
        )}
        {!error && !loading && engines.length === 0 && (
          <div className="quiet-state">
            <strong>没有匹配的引擎</strong>
            <span>调整搜索条件，或新建一项引擎资产。</span>
          </div>
        )}
        {engines.map((engine) => (
          <EngineRow
            key={engine.id}
            engine={engine}
            selected={selectedId === engine.id}
            onSelect={() => onSelect(engine)}
            onEdit={() => onEdit(engine)}
            onDelete={() => onDelete(engine)}
          />
        ))}
      </div>

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
