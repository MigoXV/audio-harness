import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AudioHarnessApi, EngineAsset, EngineKind, EngineWritePayload } from "../api/types";
import { errorMessage } from "../api/client";
import { EngineEditor } from "../features/engines/EngineEditor";
import { EngineDetails, EngineLifecycleDialog } from "../features/engines/EngineDetails";
import { dateLabel, healthGroup, healthLabels, kindMeta, kinds, readCatalog, statusLabel } from "../features/engines/model";
import type { HealthFilter } from "../features/engines/model";
import "../features/engines/console.css";

interface Props { api: AudioHarnessApi; refreshVersion: number; }
const EMPTY: EngineAsset[] = [];
const catalogKey = ["engine-console-list"];

export function EngineConsolePage({ api, refreshVersion }: Props) {
  const client = useQueryClient();
  const catalog = useQuery({ queryKey: catalogKey, queryFn: ({ signal }) => readCatalog(api, signal), retry: false, placeholderData: previous => previous });
  const refetchCatalog = catalog.refetch;
  const previousRefresh = useRef(refreshVersion);
  useEffect(() => {
    if (previousRefresh.current === refreshVersion) return;
    previousRefresh.current = refreshVersion;
    void refetchCatalog();
  }, [refreshVersion, refetchCatalog]);
  const items = catalog.data ?? EMPTY;
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<EngineKind | "all">("all");
  const [health, setHealth] = useState<HealthFilter>("all");
  const [sort, setSort] = useState("created");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<EngineAsset | null>(null);
  const [editing, setEditing] = useState<EngineAsset | "new" | null>(null);
  const [lifecycle, setLifecycle] = useState<{ engine: EngineAsset; action: "disable" | "delete" } | null>(null);
  const [pending, setPending] = useState<Set<string>>(() => new Set());
  const locks = useRef(new Set<string>());
  const batchLock = useRef(false);
  const [batch, setBatch] = useState(false);
  const [messages, setMessages] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const origin = useRef<HTMLElement | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const upsert = useCallback((item: EngineAsset) => {
    client.setQueryData<EngineAsset[]>(catalogKey, previous => {
      const current = previous ?? [];
      return current.some(value => value.id === item.id) ? current.map(value => value.id === item.id ? item : value) : [...current, item];
    });
  }, [client]);
  const persist = (item: EngineAsset) => { upsert(item); client.setQueryData(["engine-console-detail", item.id], item); };
  const run = async <T,>(id: string, job: () => Promise<T>): Promise<T> => {
    if (batchLock.current || locks.current.has(id)) throw new Error("该引擎正在处理，请稍后再试。");
    locks.current.add(id); setPending(new Set(locks.current));
    try {
      await client.cancelQueries({ queryKey: catalogKey, exact: true });
      await client.cancelQueries({ queryKey: ["engine-console-detail", id], exact: true });
      return await job();
    }
    finally { locks.current.delete(id); setPending(new Set(locks.current)); }
  };
  const probe = async (item: EngineAsset) => {
    try {
      await run(item.id, async () => {
        setMessages(current => ({ ...current, [item.id]: "正在复检…" }));
        const result = await api.probeEngine(item.id);
        const outcome = result.outcome === "passed" ? "复检通过" : result.outcome === "stale" ? "配置已变更，探测结果未写入" : "复检未通过";
        setMessages(current => ({ ...current, [item.id]: `${outcome}${result.message ? `：${result.message}` : ""}` }));
        try { await client.cancelQueries({ queryKey: ["engine-console-detail", item.id], exact: true }); persist(await api.getEngine(item.id)); }
        catch (reason) { setMessages(current => ({ ...current, [item.id]: `${outcome}；最新信息读取失败，以下为旧信息。${errorMessage(reason)}` })); }
      });
    } catch (reason) { setMessages(current => ({ ...current, [item.id]: errorMessage(reason, "引擎复检请求失败，请重试。") })); }
  };
  const probeAll = async () => {
    if (batchLock.current || locks.current.size) return;
    batchLock.current = true; setBatch(true); setNotice(null);
    try {
      await client.cancelQueries({ queryKey: catalogKey, exact: true });
      const result = await api.validateEngines();
      const count = (outcome: string) => result.results.filter(value => value.outcome === outcome).length;
      const summary = `全部复检完成：通过 ${count("passed")}，失败 ${count("failed")}，配置变更 ${count("stale")}。`;
      setMessages(current => ({ ...current, ...Object.fromEntries(result.results.map(value => [value.engine_id, `${value.outcome === "passed" ? "复检通过" : value.outcome === "stale" ? "配置已变更，探测结果未写入" : "复检未通过"}${value.message ? `：${value.message}` : ""}`])) }));
      const refreshed = await catalog.refetch();
      setNotice(refreshed.isError ? `${summary}最新目录读取失败，当前显示旧信息，请刷新列表。` : summary);
      if (!refreshed.isError) await client.invalidateQueries({ queryKey: ["engine-console-detail"] });
    } catch (reason) { setNotice(errorMessage(reason, "全部复检请求失败，请重试。")); }
    finally { batchLock.current = false; setBatch(false); }
  };
  const save = (payload: EngineWritePayload | Partial<EngineWritePayload>) => run(editing && editing !== "new" ? editing.id : "new", async () => {
    const item = editing && editing !== "new" ? await api.updateEngine(editing.id, payload) : await api.createEngine(payload as EngineWritePayload);
    persist(item);
    if (editing === "new") void client.invalidateQueries({ queryKey: catalogKey, exact: true });
    return item;
  });
  const restoreFocus = () => {
    const target = origin.current;
    requestAnimationFrame(() => {
      // A quick follow-up action may already have moved focus or opened another drawer.
      const active = document.activeElement;
      if (active && active !== document.body && active !== document.documentElement && active !== target) return;
      if (target?.isConnected && !(target as HTMLButtonElement).disabled) target.focus();
      else headingRef.current?.focus();
    });
  };
  const remember = () => { origin.current = document.activeElement as HTMLElement; };
  const openEdit = (item: EngineAsset) => { if (!selected) remember(); setSelected(null); setEditing(item); };
  const changeLifecycle = async (item: EngineAsset, action: "enable" | "disable" | "delete") => {
    if (action !== "enable") { setLifecycle({ engine: item, action }); return; }
    try { await run(item.id, async () => { persist(await api.updateEngine(item.id, { enabled: true })); setMessages(current => ({ ...current, [item.id]: "引擎已启用，等待实际健康探测。" })); }); }
    catch (reason) { setMessages(current => ({ ...current, [item.id]: errorMessage(reason, "引擎启用失败。") })); }
  };
  const confirmLifecycle = async () => {
    if (!lifecycle) return;
    await run(lifecycle.engine.id, async () => {
      if (lifecycle.action === "delete") {
        await api.deleteEngine(lifecycle.engine.id);
        client.setQueryData<EngineAsset[]>(catalogKey, previous => previous?.filter(value => value.id !== lifecycle.engine.id));
        client.removeQueries({ queryKey: ["engine-console-detail", lifecycle.engine.id], exact: true });
        setSelected(null); setNotice(`已删除 ${lifecycle.engine.name}。`);
      } else { persist(await api.updateEngine(lifecycle.engine.id, { enabled: false })); setMessages(current => ({ ...current, [lifecycle.engine.id]: "引擎已停用。" })); }
      setLifecycle(null);
    });
  };
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    const values = items.filter(item => (kind === "all" || item.kind === kind) && (health === "all" || healthGroup(item) === health) && `${item.name} ${item.slug} ${item.target}`.toLocaleLowerCase().includes(needle));
    if (sort === "name") values.sort((a, b) => a.name.localeCompare(b.name, "zh-CN") || a.id.localeCompare(b.id));
    if (sort === "updated") values.sort((a, b) => b.updated_at.localeCompare(a.updated_at) || a.id.localeCompare(b.id));
    return values;
  }, [items, kind, health, query, sort]);
  const pages = Math.max(1, Math.ceil(filtered.length / 50));
  const currentPage = Math.min(page, pages);
  const visible = filtered.slice((currentPage - 1) * 50, currentPage * 50);
  const clear = () => { setQuery(""); setKind("all"); setHealth("all"); setPage(1); };
  const countsReady = Boolean(catalog.data) && !catalog.isError && !catalog.isPlaceholderData;
  const selectedItem = selected ? items.find(item => item.id === selected.id) ?? selected : null;
  return <main className="workspace-page ec-console">
    <header className="page-heading"><div><h1 ref={headingRef} tabIndex={-1}>推理引擎</h1><p>注册、配置连接，查看健康诊断与处理链引用。</p></div><div className="ec-page-actions"><button className="secondary-button" disabled={catalog.isFetching || batch || pending.size > 0} onClick={() => void catalog.refetch()}>{catalog.isFetching && catalog.data ? "刷新中…" : "刷新列表"}</button><button className="secondary-button" disabled={catalog.isFetching || batch || pending.size > 0 || !items.some(item => item.enabled)} onClick={() => void probeAll()}>{batch ? "全部复检中…" : "复检全部已启用引擎"}</button><button className="primary-button" disabled={batch || catalog.isFetching} onClick={() => { remember(); setEditing("new"); }}>注册引擎</button></div></header>
    {catalog.isError && <div className="alert alert-error" role="alert">{errorMessage(catalog.error, "引擎列表读取失败。")}{catalog.data && <p>当前显示上次读取的目录，统计暂不可用。</p>}<button className="secondary-button" disabled={catalog.isFetching || batch || pending.size > 0} onClick={() => void catalog.refetch()}>重新加载</button></div>}
    {notice && <p className="ec-operation-message" role="status">{notice}</p>}{batch && <p role="status">正在复检全部已启用引擎，完成后更新诊断结果…</p>}
    <section className="ec-health" aria-label="引擎健康筛选">{(Object.keys(healthLabels) as HealthFilter[]).map(value => {
      const count = countsReady ? value === "all" ? items.length : items.filter(item => healthGroup(item) === value).length : null;
      const needsAttention = value === "unhealthy" && count !== null && count > 0;
      return <button key={value} className={needsAttention ? "ec-health-attention" : undefined} aria-pressed={health === value} onClick={() => { setHealth(value); setPage(1); }}>{needsAttention && <span className="ec-health-icon" aria-hidden="true">⚠</span>}<span>{healthLabels[value]}</span><strong>{count ?? "—"}</strong></button>;
    })}</section>
    <section className="ec-filters" aria-label="引擎筛选"><label><span className="ec-visually-hidden">搜索引擎</span><input value={query} onChange={event => { setQuery(event.target.value); setPage(1); }} placeholder="搜索名称、Slug 或地址" /></label><label><span className="ec-visually-hidden">能力类型</span><select value={kind} onChange={event => { setKind(event.target.value as EngineKind | "all"); setPage(1); }}><option value="all">全部能力</option>{kinds.map(value => <option key={value} value={value}>{kindMeta[value].label}</option>)}</select></label><label><span className="ec-visually-hidden">排序</span><select value={sort} onChange={event => { setSort(event.target.value); setPage(1); }}><option value="created">创建顺序</option><option value="name">名称</option><option value="updated">最近更新</option></select></label>{(query || kind !== "all" || health !== "all") && <button className="text-button ec-clear-filters" onClick={clear}>清空筛选</button>}{catalog.data && <p className="ec-result-count">{countsReady ? `${filtered.length} 个引擎${pages > 1 ? ` · 第 ${currentPage} / ${pages} 页` : ""}` : "上次读取的引擎目录"}{catalog.isFetching && " · 正在刷新"}</p>}</section>
    {catalog.isPending ? <div className="empty-state" role="status">正在加载引擎…</div> : !catalog.data ? <div className="empty-state">引擎列表暂时无法读取，请重新加载。</div> : items.length === 0 ? <div className="empty-state"><strong>尚未注册引擎</strong><p>先注册连接地址，再复检实际连通性。</p></div> : filtered.length === 0 ? <div className="empty-state"><strong>没有符合条件的引擎</strong><button className="secondary-button" onClick={clear}>清空筛选</button></div> : <>
      <table className="ec-table" aria-label="引擎列表"><thead><tr><th scope="col">名称 / Slug</th><th scope="col">能力</th><th scope="col" className="ec-address">地址</th><th scope="col">健康状态</th><th scope="col" className="ec-observed">最近探测</th><th scope="col">操作</th></tr></thead><tbody>{visible.map(item => <Fragment key={item.id}><tr>
        <td><button className="ec-name" aria-label={item.name} aria-haspopup="dialog" onClick={() => { remember(); setSelected(item); }}><strong>{item.name}</strong><small>{item.slug}</small></button></td><td className="ec-kind">{kindMeta[item.kind].label}</td><td className="ec-address"><code title={item.target}>{item.target}</code></td><td className="ec-status"><span className={`status-chip engine-${!item.enabled ? "disabled" : item.status}`}>{statusLabel(item)}</span></td><td className="ec-observed">{dateLabel(item.status_observed_at)}</td><td className="ec-row-actions"><button className="text-button" aria-label={`编辑 ${item.name}`} disabled={batch || pending.has(item.id)} onClick={() => openEdit(item)}>编辑</button><button className="text-button" aria-label={`复检 ${item.name}`} disabled={batch || pending.has(item.id) || !item.enabled} title={!item.enabled ? "引擎已停用，无法复检" : undefined} onClick={() => void probe(item)}>{pending.has(item.id) ? "处理中…" : "复检"}</button></td>
      </tr>{messages[item.id] && <tr className="ec-message-row"><td className="ec-row-message" colSpan={6}><p role="status">{messages[item.id]}</p></td></tr>}</Fragment>)}</tbody></table>
      {pages > 1 && <nav className="ec-pagination" aria-label="引擎分页"><button className="secondary-button" disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)}>上一页</button><span>第 {currentPage} / {pages} 页</span><button className="secondary-button" disabled={currentPage === pages} onClick={() => setPage(currentPage + 1)}>下一页</button></nav>}
    </>}
    {selectedItem && <EngineDetails key={selectedItem.id} api={api} initial={selectedItem} busy={batch || pending.has(selectedItem.id)} message={messages[selectedItem.id]} onUpdated={upsert} onEdit={openEdit} onProbe={item => void probe(item)} onLifecycle={(item, action) => void changeLifecycle(item, action)} onClose={() => setSelected(null)} restoreFocus={restoreFocus} />}
    {editing && <EngineEditor key={editing === "new" ? "new" : editing.id} engine={editing === "new" ? null : editing} initialKind={kind === "all" ? "asr" : kind} blocked={batch} onSave={save} onSaved={(item, withProbe) => { setEditing(null); setNotice(`已保存 ${item.name}。`); if (withProbe) { setSelected(item); void probe(item); } }} onClose={() => setEditing(null)} restoreFocus={restoreFocus} />}
    {lifecycle && <EngineLifecycleDialog key={`${lifecycle.engine.id}-${lifecycle.action}`} api={api} engine={lifecycle.engine} action={lifecycle.action} busy={pending.has(lifecycle.engine.id)} onConfirm={confirmLifecycle} onClose={() => setLifecycle(null)} />}
  </main>;
}
