import { globalStyle } from "@vanilla-extract/css";
import { vars } from "../styles/theme.css";

const desktop = "screen and (min-width: 1101px)";
const compact = "screen and (max-width: 1100px)";
const workspace = ".workflow-detail-page";

// 阶段导航属于工作流对象；各阶段共享位置，画布和记录各自滚动。
globalStyle(`${workspace}.workspace-page`, { width: "100%", maxWidth: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", height: "calc(100dvh - 64px)", minHeight: 0 });
globalStyle(".workflow-phase-header", { display: "flex", alignItems: "center", flexShrink: 0, height: 52, borderBottom: `1px solid ${vars.color.line}`, background: vars.color.surfaceMuted });
globalStyle(".workflow-phase-header > strong", { flex: "0 0 200px", minWidth: 0, paddingInline: 20, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 16, fontWeight: 500 });
globalStyle(".workflow-phase-header .builder-detail-navigation", { display: "flex", alignSelf: "stretch", gap: 36, padding: "0 24px", border: 0 });
globalStyle(".workflow-phase-header .builder-detail-navigation a", { position: "relative", display: "flex", alignItems: "center", justifyContent: "center", minWidth: 40, padding: "0 6px", border: 0, borderRadius: 0, background: "transparent", fontSize: 14, fontWeight: 400, color: vars.color.textMuted });
globalStyle(".workflow-phase-header .builder-detail-navigation a.active", { background: "transparent", color: vars.color.text, fontWeight: 500 });
globalStyle(".workflow-phase-header .builder-detail-navigation a.active::after", { content: '""', position: "absolute", bottom: 0, width: 30, height: 2, background: vars.color.accent });
globalStyle(`${workspace} > .workflow-detail-layout, ${workspace} > .workflow-builder`, { flex: 1, minHeight: 0, height: "auto", gap: 0 });
globalStyle(`${workspace} > .workflow-detail-layout.palette-open`, { gridTemplateColumns: "200px minmax(0, 1fr)", "@media": { [compact]: { gridTemplateColumns: "52px minmax(0, 1fr)" } } });
globalStyle(`${workspace} .workbench-sidebar`, { width: "100%", border: 0, borderRight: `1px solid ${vars.color.line}`, borderRadius: 0, background: vars.color.surfaceMuted });
globalStyle(`${workspace} .palette-open .workbench-sidebar > header`, { "@media": { [desktop]: { display: "none" } } });
globalStyle(`${workspace} .workflow-detail-layout .workbench-sidebar-body`, { padding: "28px 20px 12px", height: "100%", gap: 0 });
globalStyle(`${workspace} .builder-workflow-summary`, { border: 0, padding: 0 });
globalStyle(`${workspace} .builder-back-link`, { marginBottom: 28, fontSize: 12, color: vars.color.textMuted, textDecoration: "none" });
globalStyle(`${workspace} .builder-workflow-title h1`, { fontSize: 16, fontWeight: 500 });
globalStyle(`${workspace} .builder-workflow-summary p`, { margin: "6px 0 0" });
globalStyle(".workflow-context-facts", { margin: "20px 0 0", paddingTop: 12, borderTop: `1px solid ${vars.color.line}` });
globalStyle(".workflow-context-facts > div", { display: "flex", justifyContent: "space-between", gap: 12, minHeight: 28, alignItems: "center", fontSize: 12 });
globalStyle(".workflow-context-facts dt, .workflow-release-facts dt", { color: vars.color.textMuted });
globalStyle(".workflow-context-facts dd, .workflow-release-facts dd", { margin: 0, fontFamily: vars.font.mono, fontSize: 12 });
globalStyle(`${workspace} .workflow-sidebar-footer`, { borderTop: `1px solid ${vars.color.line}`, paddingTop: 16 });
globalStyle(`${workspace} .workflow-sidebar-footer .builder-save-state`, { padding: 0, marginBottom: 12 });
globalStyle(`${workspace} .workflow-sidebar-footer .builder-save-state strong`, { fontSize: 12, fontWeight: 400 });
globalStyle(`${workspace} .workflow-sidebar-footer .builder-save-state small`, { display: "none" });
globalStyle(`${workspace} .workflow-sidebar-footer > button`, { width: "100%", minHeight: 30, padding: "4px 8px", fontWeight: 400 });
globalStyle(`${workspace} .workflow-detail-content`, { minHeight: 0, border: 0, borderRadius: 0, padding: "42px clamp(24px, 4.5vw, 64px) 48px", background: vars.color.canvas });
globalStyle(`${workspace} .workflow-detail-content > *`, { width: "100%", maxWidth: 1120 });
globalStyle(`${workspace} .workflow-detail-content .panel-heading`, { padding: 0, border: 0 });
globalStyle(`${workspace} .workflow-detail-content .panel-heading h2`, { marginBottom: 12, fontSize: 23, fontWeight: 500, lineHeight: 1.25 });
globalStyle(`${workspace} .workflow-detail-content .panel-heading p`, { fontSize: 13 });
globalStyle(`${workspace} .workflow-detail-content .primary-button, ${workspace} .workflow-detail-content .secondary-button`, { fontSize: 14, fontWeight: 400, borderRadius: 8 });
globalStyle(".workflow-release-facts", { display: "flex", gap: 100, padding: "32px 0 20px", margin: 0, borderBottom: `1px solid ${vars.color.line}`, flexWrap: "wrap" });
globalStyle(".workflow-release-facts > div", { display: "flex", alignItems: "center", gap: 48, fontSize: 12 });
globalStyle(`${workspace} .publish-review .success-block`, { display: "flex", gap: 10, alignItems: "center", margin: "24px 0 16px", padding: 0, fontSize: 14, fontWeight: 400 });
globalStyle(`${workspace} .publish-review .success-block::before`, { content: '""', width: 6, height: 6, flexShrink: 0, borderRadius: "50%", background: vars.color.success });
globalStyle(`${workspace} .publish-review .panel-actions`, { border: 0, padding: 0, marginBottom: 52 });
globalStyle(`${workspace} .deployment-card`, { paddingTop: 24, borderTop: `1px solid ${vars.color.line}` });
globalStyle(`${workspace} .deployment-card .eyebrow, ${workspace} .deployment-card .status-chip`, { display: "none" });
globalStyle(`${workspace} .deployment-card .panel-heading h2`, { fontSize: 18, marginBottom: 8 });
globalStyle(`${workspace} .endpoint-examples`, { display: "block", padding: 0, marginTop: 24, border: 0 });
globalStyle(`${workspace} .endpoint-examples label`, { display: "grid", gridTemplateColumns: "minmax(160px, 22%) minmax(0, 1fr)", minHeight: 48, alignItems: "center", borderBottom: `1px solid ${vars.color.line}`, fontWeight: 400 });
globalStyle(`${workspace} .endpoint-examples label > span`, { color: vars.color.textMuted });
globalStyle(`${workspace} .endpoint-examples code`, { padding: 0, background: "transparent", fontSize: 14 });
globalStyle(".workflow-history-table", { width: "100%", borderCollapse: "collapse", marginTop: 32, textAlign: "left", fontSize: 13 });
globalStyle(".workflow-history-table th", { height: 48, fontSize: 12, fontWeight: 400, color: vars.color.textMuted, borderBlock: `1px solid ${vars.color.line}` });
globalStyle(".workflow-history-table th:nth-child(1), .workflow-history-table th:nth-child(2)", { width: "22%" });
globalStyle(".workflow-history-table td", { padding: "16px 0", borderBottom: `1px solid ${vars.color.line}` });
globalStyle(".workflow-history-table td small", { display: "block", color: vars.color.textMuted });
globalStyle(".workflow-history-empty", { padding: "16px 0 8px" });
globalStyle(".workflow-history-empty strong", { fontSize: 14, fontWeight: 500 });
globalStyle(".workflow-history-empty p", { margin: "8px 0 0", fontSize: 13 });

globalStyle(".workflow-list-page .page-heading", { paddingBottom: 16 });
globalStyle(".workflow-list-page .object-list-panel > .panel-heading", { padding: "0 0 10px", minHeight: 50, justifyContent: "flex-end", border: 0 });
globalStyle(".workflow-list-page .status-chip, .workflow-list-page .exposure-badges code", { padding: 0, background: "transparent", fontFamily: vars.font.mono, fontWeight: 400 });
globalStyle(".workflow-list-page .workflow-row", { gridTemplateColumns: "1.5fr 1fr .6fr .8fr 1.2fr 20px" });

globalStyle(`${workspace} .test-console`, { display: "flex", flexDirection: "column", gap: 0 });
globalStyle(`${workspace} .test-control-panel .test-status`, { margin: "8px 0", fontSize: 12, minHeight: 0 });
globalStyle(`${workspace} .test-control-panel .panel-actions`, { border: 0, padding: "16px 0 32px" });
globalStyle(`${workspace} .conversation-grid`, { margin: 0, padding: "20px 0 32px", minHeight: 194, borderBlock: `1px solid ${vars.color.line}` });
globalStyle(`${workspace} .conversation-card`, { padding: 0, border: 0, background: "transparent" });
globalStyle(`${workspace} .conversation-label`, { marginBottom: 28, fontSize: 14, fontWeight: 500 });
globalStyle(`${workspace} .conversation-card p`, { fontSize: 14 });
globalStyle(`${workspace} .workflow-diagnostics-heading`, { margin: "24px 0 20px", fontSize: 16, fontWeight: 500 });
globalStyle(`${workspace} .latency-grid`, { gap: "0 64px", margin: 0, padding: 0 });
globalStyle(`${workspace} .metric-card`, { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, minHeight: 46, padding: 0, border: 0, borderBottom: `1px solid ${vars.color.line}`, background: "transparent" });
globalStyle(`${workspace} .metric-card span, ${workspace} .metric-card strong`, { fontSize: 13, fontWeight: 400 });
globalStyle(`${workspace} .event-log`, { marginTop: 40, paddingTop: 20, borderTop: `1px solid ${vars.color.line}` });
globalStyle(`${workspace} .event-log summary`, { padding: 0, fontSize: 14 });
globalStyle(`${workspace} .workflow-detail-layout .workbench-sidebar-footer`, { marginTop: "auto", padding: 0, position: "static", border: 0, background: "transparent" });
globalStyle(`${workspace} .workbench-library-context`, { padding: "24px 20px 12px" });
globalStyle(`${workspace} .workflow-builder .builder-back-link`, { margin: 0 });
globalStyle(`${workspace} .workflow-toolbar`, { minHeight: 72, flexShrink: 0, padding: "16px 24px", borderBottom: `1px solid ${vars.color.line}` });
globalStyle(`${workspace} .workflow-toolbar h2`, { margin: 0, fontSize: 16, fontWeight: 500 });
globalStyle(`${workspace} .workflow-toolbar > .icon-button`, { "@media": { [desktop]: { display: "none" } } });
globalStyle(`${workspace} .workflow-canvas-footer`, { margin: "0 24px", padding: "16px 0 20px", borderTop: `1px solid ${vars.color.line}`, flexShrink: 0 });
globalStyle(`${workspace} .workflow-canvas-footer .workflow-type-legend`, { position: "static", margin: 0, gap: "12px 28px" });
globalStyle(`${workspace} .workflow-canvas-footer .workflow-type-legend .workflow-port-marker`, { display: "none" });
globalStyle(`${workspace} .workflow-canvas-footer p`, { margin: "20px 0 0", fontSize: 12 });
globalStyle(".workflow-test-console-link", { display: "flex", alignItems: "center", gap: 12, flexShrink: 0, minHeight: 56, padding: "12px 24px", borderTop: `1px solid ${vars.color.line}`, color: vars.color.text, fontSize: 13, textDecoration: "none" });
globalStyle(".workflow-test-console-link small", { color: vars.color.textMuted, fontSize: 12 });
globalStyle(".workflow-test-console-link:hover", { background: vars.color.hover });
globalStyle(".workflow-test-console-link::after", { content: '"⌃"', marginLeft: "auto", color: vars.color.textMuted });
globalStyle(`${workspace} .workflow-node-library > header`, { marginBottom: 16 });
globalStyle(`${workspace} .workflow-node-library .workflow-template-trigger`, { fontSize: 11 });
globalStyle(`${workspace} .workflow-graph-node`, { borderColor: vars.color.lineStrong });
globalStyle(`${workspace} .workflow-graph-node.selected`, { borderColor: vars.color.accent, background: vars.color.selected, boxShadow: "none" });

globalStyle(".workflow-phase-header > strong", { "@media": { [compact]: { flexBasis: 160, paddingInline: 14 }, "screen and (max-width: 550px)": { flexBasis: 112, fontSize: 14 } } });
globalStyle(".workflow-phase-header .builder-detail-navigation", { "@media": { [compact]: { flex: 1, gap: 8, justifyContent: "space-around", padding: "0 8px" } } });
globalStyle(`${workspace} .workflow-detail-content`, { "@media": { "screen and (max-width: 700px)": { padding: "24px 16px 36px" } } });
globalStyle(".workflow-release-facts", { "@media": { "screen and (max-width: 700px)": { gap: 20 } } });
globalStyle(`${workspace} .endpoint-examples label`, { "@media": { "screen and (max-width: 700px)": { gridTemplateColumns: "1fr", gap: 8, padding: "12px 0" } } });
globalStyle(`${workspace} .workflow-toolbar-actions`, { gap: 12, flexWrap: "wrap" });
globalStyle(`${workspace} .workflow-detail-content.object-history-content .object-records`, { overflowX: "auto" });
globalStyle(".workflow-history-table", { "@media": { "screen and (max-width: 700px)": { minWidth: 480 } } });

// 紧凑诊断行在窄屏改为单列；内容状态变化不撑开整个工作区。
globalStyle(`${workspace} .latency-grid.transcription-only`, { gridTemplateColumns: "repeat(2, minmax(0, 1fr))", "@media": { "screen and (max-width: 700px)": { gridTemplateColumns: "minmax(0, 1fr)" } } });
globalStyle(`${workspace} .test-status-idle`, { display: "none" });
globalStyle(`${workspace} .test-control-panel .test-status:not(.test-status-idle)`, { padding: "8px 0", border: 0, background: "transparent" });
globalStyle(`${workspace} .test-connection-options`, { marginTop: 24 });
globalStyle(`${workspace} .connection-indicator`, { display: "inline-flex", alignItems: "center", gap: 8, color: vars.color.textMuted, fontSize: 12 });
globalStyle(`${workspace} .connection-indicator::before`, { content: '""', width: 6, height: 6, borderRadius: "50%", background: "currentColor" });
globalStyle(`${workspace} .workflow-detail-layout .builder-workflow-summary, ${workspace} .workflow-sidebar-footer`, { minWidth: 0, width: "100%" });
globalStyle(`${workspace} .workflow-detail-layout .workbench-sidebar-body`, { overflowX: "hidden" });
globalStyle(".workflow-context-facts > div > dd", { flexShrink: 0 });

// 新版 Figma 的类型家族色独立于品牌和业务状态色，端口、连线、图例共用。
globalStyle(workspace, { vars: {
  "--workflow-port-audioStream": "#3786A0", "--workflow-port-audioChunk": "#568368",
  "--workflow-port-control": "#AC7A32", "--workflow-port-embedding": "#83719E",
  "--workflow-port-result": "#687A87", "--workflow-port-unknown": "#858D89",
} });
globalStyle(`[data-theme="abyssus"] ${workspace}`, { vars: {
  "--workflow-port-audioStream": "#62ADC4", "--workflow-port-audioChunk": "#80B596",
  "--workflow-port-control": "#C7A05C", "--workflow-port-embedding": "#AB97C5",
  "--workflow-port-result": "#99ADBC", "--workflow-port-unknown": "#7C8B98",
} });
globalStyle(`${workspace} .workflow-chain-heading`, { display: "flex", alignItems: "center", gap: 24, minHeight: 64, margin: "0 24px", padding: "16px 0", borderBottom: `1px solid ${vars.color.line}`, flexShrink: 0, fontSize: 14 });
globalStyle(`${workspace} .workflow-chain-heading strong`, { fontWeight: 500 });
globalStyle(`${workspace} .workflow-chain-heading span`, { color: vars.color.textMuted, fontSize: 12 });
globalStyle(`${workspace} .workflow-type-legend > span > span:last-child`, { color: vars.color.textMuted, fontSize: 11 });

// 次级表面随新版背景映射，白垣保留温和的米灰侧栏。
globalStyle(`${workspace} .workflow-builder .workbench-sidebar-footer`, { background: vars.color.surfaceMuted });
globalStyle(`[data-theme="abyssus"] .workflow-phase-header, [data-theme="abyssus"] ${workspace} .workbench-sidebar, [data-theme="abyssus"] ${workspace} .workflow-builder .workbench-sidebar-footer`, { background: vars.color.canvas });
