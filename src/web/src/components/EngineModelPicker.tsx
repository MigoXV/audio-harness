import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi, EngineAsset } from "../api/types";

const EMPTY_MODELS: string[] = [];

interface Props {
  api: AudioHarnessApi;
  engine: EngineAsset | undefined;
  value: string;
  label?: string;
  onChange: (value: string) => void;
}

export function EngineModelPicker({ api, engine, value, label = "Responses 模型", onChange }: Props) {
  const inputId = useId();
  const listId = `${inputId}-options`;
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [openForEngineId, setOpenForEngineId] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState(-1);
  const models = useQuery({
    queryKey: ["engine-models", engine?.id, engine?.revision],
    queryFn: ({ signal }) => api.listEngineModels(engine!.id, signal),
    enabled: Boolean(engine),
    retry: false,
    staleTime: 60_000,
  });
  const items = models.data?.items ?? EMPTY_MODELS;
  const open = Boolean(engine && openForEngineId === engine.id);
  const visibleItems = useMemo(() => {
    const query = value.trim().toLocaleLowerCase();
    if (!query || items.some((item) => item.toLocaleLowerCase() === query)) return items;
    return items.filter((item) => item.toLocaleLowerCase().includes(query));
  }, [items, value]);

  useEffect(() => {
    if (!value && items.length === 1) onChange(items[0]);
  }, [items, onChange, value]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpenForEngineId(null);
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    return () => document.removeEventListener("mousedown", closeOnOutsideClick);
  }, [open]);

  const showOptions = () => {
    if (!engine || items.length === 0) return;
    setOpenForEngineId(engine.id);
    setActiveIndex((current) => current >= 0 && current < visibleItems.length ? current : 0);
  };

  const selectItem = (item: string) => {
    onChange(item);
    setOpenForEngineId(null);
    setActiveIndex(-1);
    inputRef.current?.focus();
  };

  let status = "先选择 Responses 引擎";
  if (engine && models.isFetching && !models.data) {
    status = `正在从 ${engine.name} 获取模型…`;
  } else if (engine && models.isError) {
    status = `${errorMessage(models.error, "模型列表读取失败。")} 可继续手动输入。`;
  } else if (engine && models.data && items.length === 0) {
    status = "引擎没有报告可用模型，可继续手动输入。";
  } else if (engine && models.data && value && !items.includes(value)) {
    status = "当前值未出现在最新列表中，可继续作为手填值。";
  } else if (engine && models.data) {
    status = `已从 ${engine.name} 获取 ${items.length} 个模型。`;
  }

  return <div
    ref={rootRef}
    className="engine-model-picker"
    onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpenForEngineId(null);
    }}
  >
    <label htmlFor={inputId}>{label}</label>
    <div className="engine-model-picker-controls">
      <div className="engine-model-input-shell">
        <input
          ref={inputRef}
          id={inputId}
          role="combobox"
          aria-autocomplete="list"
          aria-controls={listId}
          aria-expanded={open}
          aria-activedescendant={open && activeIndex >= 0 ? `${listId}-${activeIndex}` : undefined}
          disabled={!engine}
          value={value}
          onChange={(event) => {
            onChange(event.target.value);
            if (engine && items.length > 0) {
              setOpenForEngineId(engine.id);
              setActiveIndex(0);
            }
          }}
          onFocus={showOptions}
          onClick={showOptions}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" && visibleItems.length > 0) {
              event.preventDefault();
              if (engine) setOpenForEngineId(engine.id);
              setActiveIndex((current) => open ? Math.min(current + 1, visibleItems.length - 1) : 0);
            } else if (event.key === "ArrowUp" && visibleItems.length > 0) {
              event.preventDefault();
              if (engine) setOpenForEngineId(engine.id);
              setActiveIndex((current) => open && current >= 0 ? Math.max(current - 1, 0) : visibleItems.length - 1);
            } else if (event.key === "Enter" && open && activeIndex >= 0 && visibleItems[activeIndex]) {
              event.preventDefault();
              selectItem(visibleItems[activeIndex]);
            } else if (event.key === "Escape") {
              setOpenForEngineId(null);
              setActiveIndex(-1);
            }
          }}
          placeholder={engine ? "选择或输入模型" : "先选择引擎"}
        />
        <button
          type="button"
          className="engine-model-toggle"
          aria-label={open ? "收起模型列表" : "展开模型列表"}
          aria-expanded={open}
          disabled={!engine || items.length === 0}
          onClick={() => {
            if (open) {
              setOpenForEngineId(null);
            } else {
              showOptions();
              inputRef.current?.focus();
            }
          }}
        >
          <span aria-hidden="true">⌄</span>
        </button>
        {open && <ul id={listId} className="engine-model-options" role="listbox" aria-label="可用 Responses 模型">
          {visibleItems.length > 0 ? visibleItems.map((item, index) => <li
            key={item}
            id={`${listId}-${index}`}
            role="option"
            aria-selected={item === value}
            className={index === activeIndex ? "active" : undefined}
            onMouseDown={(event) => event.preventDefault()}
            onMouseEnter={() => setActiveIndex(index)}
            onClick={() => selectItem(item)}
          >
            <span>{item}</span>
            {item === value && <span className="engine-model-selected" aria-hidden="true">✓</span>}
          </li>) : <li className="engine-model-empty">没有匹配项，可继续手动输入</li>}
        </ul>}
      </div>
      <button
        type="button"
        className="secondary-button"
        disabled={!engine || models.isFetching}
        onClick={() => void models.refetch()}
      >
        {models.isFetching ? "获取中…" : "重新获取"}
      </button>
    </div>
    <small role="status" aria-live="polite">{status}</small>
  </div>;
}
