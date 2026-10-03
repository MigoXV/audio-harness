import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { NavLink, useLocation } from "react-router-dom";

import type { RuntimeOverview } from "../api/types";
import { applyTheme, persistTheme, readTheme } from "../styles/themePreference";

const groups: ReadonlyArray<{ name: string; items: ReadonlyArray<readonly [string, string]> }> = [
  { name: "编排", items: [["/audio-workflows", "语音工作流"], ["/voice-agents", "语音智能体"]] },
  { name: "资源", items: [["/engines", "推理引擎"], ["/agent-backends", "后端智能体"], ["/vectors", "向量检索"]] },
  { name: "运行", items: [["/runtime", "接入与运行"]] },
];
const items = groups.flatMap((group) => group.items);
const resourceItems = items.slice(3, 5);

interface Props {
  overview: RuntimeOverview | null;
  failed: boolean;
  onRefresh: () => void;
}

function statusText(overview: RuntimeOverview | null, failed: boolean): string {
  if (failed || overview?.status === "unavailable") return "管理服务不可用";
  if (!overview) return "正在连接";
  return overview.status === "ready" ? "服务在线" : `服务在线 · ${overview.issues.length} 项待配置`;
}

export function ShellNavigation({ overview, failed, onRefresh }: Props) {
  const { pathname } = useLocation();
  const navigation = useRef<HTMLElement>(null);
  const [theme, setTheme] = useState(readTheme);
  const [mobileOpen, setMobileOpen] = useState(false);
  const current = items.find(([path]) => pathname.startsWith(path));
  const resourceActive = resourceItems.some(([path]) => pathname.startsWith(path));
  const state = failed || overview?.status === "unavailable" ? "error" : overview?.status === "ready" ? "healthy" : "warning";

  useEffect(() => { applyTheme(theme); }, [theme]);
  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (event.target instanceof Node && !navigation.current?.contains(event.target)) {
        navigation.current?.querySelectorAll("details[open]").forEach((item) => { item.removeAttribute("open"); });
      }
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, []);

  return <header className="topbar">
    <div className="topbar-main">
      <NavLink className="brand" to="/audio-workflows" aria-label="Audio Harness 首页">
        <span className="brand-mark"><img src="/audio-harness-logo.svg" alt="" /></span>
        <strong aria-hidden="true">Audio Harness</strong>
      </NavLink>
      <nav ref={navigation} className="primary-navigation" aria-label="主导航">
        {items.slice(0, 3).map(([path, label]) => <NavLink
          className="navigation-group-link"
          to={path} key={path}
        >{label}</NavLink>)}
        <details className={`navigation-group navigation-resources ${resourceActive ? "active" : ""}`} onKeyDown={(event) => {
          if (event.key === "Escape") { event.currentTarget.open = false; event.currentTarget.querySelector("summary")?.focus(); }
        }}>
          <summary>{resourceActive ? `资源 / ${current?.[1]}` : "资源"}<span aria-hidden="true">⌄</span></summary>
          <div className="navigation-popover">{resourceItems.map(([path, label]) => <NavLink key={path} to={path} onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); }}>{label}</NavLink>)}</div>
        </details>
        <NavLink className="navigation-group-link" to="/runtime">运行</NavLink>
      </nav>
    </div>
    <NavLink className="mobile-engine-link navigation-group-link" to="/engines">推理引擎</NavLink>
    <div className="topbar-meta">
      <NavLink className="status-link" to="/runtime#access" aria-label={statusText(overview, failed)}><span className={`health-dot ${state}`} aria-hidden="true" /><span>{statusText(overview, failed)}</span></NavLink>
      <button className="text-button shell-refresh" onClick={onRefresh}>刷新</button>
      <button className="theme-toggle" aria-label={`切换到${theme === "vallum" ? "苍渊" : "白垣"}主题`} aria-pressed={theme === "abyssus"} onClick={() => {
        const next = theme === "vallum" ? "abyssus" : "vallum";
        persistTheme(next); setTheme(next);
      }}>{theme === "vallum" ? "白垣" : "苍渊"}</button>
      <Dialog.Root open={mobileOpen} onOpenChange={setMobileOpen}>
        <Dialog.Trigger className="mobile-navigation-trigger" aria-label={`打开导航，当前${current?.[1] ?? "页面"}`}>导航 <span aria-hidden="true">☰</span></Dialog.Trigger>
        <Dialog.Portal>
          <Dialog.Overlay className="drawer-backdrop" />
          <Dialog.Content className="mobile-navigation-dialog">
            <header><div><Dialog.Title>工作空间导航</Dialog.Title><Dialog.Description>选择要进入的功能区</Dialog.Description></div><Dialog.Close className="icon-button" aria-label="关闭导航">×</Dialog.Close></header>
            <nav aria-label="移动端主导航">{groups.map((group) => <section key={group.name}><h2>{group.name}</h2>{group.items.map(([path, label]) => <NavLink to={path} key={path} onClick={() => setMobileOpen(false)}>{label}</NavLink>)}</section>)}</nav>
            <footer><span>{statusText(overview, failed)}</span><button className="secondary-button" onClick={onRefresh}>刷新</button></footer>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  </header>;
}
