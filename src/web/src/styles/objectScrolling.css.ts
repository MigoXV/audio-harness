import { globalStyle } from "@vanilla-extract/css";

const desktop = "screen and (min-width: 1101px)";

// 与推理引擎一致：视口固定，滚动只属于列表记录、对象内容或节点目录。
globalStyle(".object-list-page.workspace-page, .voice-agent-create-page.workspace-page", { "@media": { [desktop]: { height: "calc(100dvh - 64px)", minHeight: 0, display: "flex", flexDirection: "column", paddingBottom: 24, overflow: "hidden" } } });
globalStyle(".object-list-page > .page-heading, .object-list-page > .alert, .object-list-panel > .panel-heading, .object-list-panel .table-header, .voice-agent-create-page > .page-heading, .voice-agent-create-page > .alert, .voice-agent-create-card > .panel-heading, .voice-agent-create-card > .panel-actions", { flexShrink: 0 });
globalStyle(".object-list-page > .object-list-panel, .voice-agent-create-page > .voice-agent-create-card", { "@media": { [desktop]: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" } } });
globalStyle(".object-list-page > .agent-onboarding", { "@media": { [desktop]: { minHeight: 0, overflowY: "auto", overscrollBehavior: "contain" } } });
globalStyle(".object-list-panel > .workflow-table", { "@media": { [desktop]: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" } } });
globalStyle(".object-records", { "@media": { [desktop]: { flex: 1, minHeight: 0, overflowY: "auto", overscrollBehavior: "contain", scrollbarGutter: "stable" } } });
globalStyle(".object-list-panel .table-header", { "@media": { [desktop]: { overflowY: "hidden", scrollbarGutter: "stable" } } });
globalStyle(".voice-agent-create-card > .form-grid", { "@media": { [desktop]: { flex: 1, minHeight: 0, alignContent: "start", overflowY: "auto", overscrollBehavior: "contain" } } });
// 创建时当前对象为表单；取消后恢复列表，避免两者争抢视口高度。
globalStyle(".object-list-page > .create-workflow-panel", { "@media": { [desktop]: { flex: 1, display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" } } });
globalStyle(".object-list-page:has(> .create-workflow-panel) > .object-list-panel", { "@media": { [desktop]: { display: "none" } } });
globalStyle(".create-workflow-panel > .panel-heading, .create-workflow-panel > .panel-actions", { flexShrink: 0 });
globalStyle(".object-list-page > .create-workflow-panel > .form-grid", { "@media": { [desktop]: { flex: 1, minHeight: 0, alignContent: "start", overflowY: "auto", overscrollBehavior: "contain" } } });

// 工作流非构建阶段也必须允许网格和内容主体缩小到视口内。
globalStyle(".workflow-sidebar-page:not(.workflow-building):not(.agent-workspace)", { "@media": { [desktop]: { height: "calc(100dvh - 64px)", minHeight: 0, overflow: "hidden" } } });
globalStyle(".workflow-sidebar-page:not(.workflow-building):not(.agent-workspace) > .workflow-detail-layout", { "@media": { [desktop]: { height: "100%", minHeight: 0 } } });
globalStyle(".workflow-sidebar-page:not(.workflow-building):not(.agent-workspace) .workbench-sidebar", { "@media": { [desktop]: { height: "100%", minHeight: 0 } } });
globalStyle(".workflow-sidebar-page:not(.workflow-building):not(.agent-workspace) .workbench-sidebar-body", { "@media": { [desktop]: { minHeight: 0, overscrollBehavior: "contain" } } });
globalStyle(".workflow-sidebar-page:not(.workflow-building):not(.agent-workspace) .workbench-sidebar-body > *", { "@media": { [desktop]: { flexShrink: 0 } } });
globalStyle(".workflow-sidebar-page:not(.workflow-building):not(.agent-workspace) .workflow-detail-content", { "@media": { [desktop]: { height: "100%", minHeight: 0, overflowY: "auto", overscrollBehavior: "contain" } } });
globalStyle(".workflow-detail-content.object-history-content", { "@media": { [desktop]: { display: "flex", flexDirection: "column", overflow: "hidden" } } });
globalStyle(".object-history-content > .object-history-panel", { "@media": { [desktop]: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column" } } });
globalStyle(".object-history-panel > .panel-heading", { flexShrink: 0 });
globalStyle(".workflow-building .workflow-stage .workflow-canvas", { "@media": { [desktop]: { minHeight: 0 } } });
globalStyle(".workflow-toolbar, .workflow-stage > .workflow-canvas-summary, .workflow-stage > .workflow-type-legend", { flexShrink: 0 });

// 智能体对象信息与保存动作固定，目录单独承接滚动。
globalStyle(".agent-workbench .agent-sidebar-content", { "@media": { [desktop]: { display: "flex", flexDirection: "column", overflow: "hidden" } } });
globalStyle(".agent-object-context", { "@media": { [desktop]: { flexShrink: 0, maxHeight: "40dvh", overflowY: "auto", overscrollBehavior: "contain" } } });
globalStyle(".agent-sidebar-content > .agent-node-library", { "@media": { [desktop]: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column" } } });
globalStyle(".agent-node-library > h2, .agent-node-library > p", { "@media": { [desktop]: { flexShrink: 0 } } });
globalStyle(".agent-library-scroll", { "@media": { [desktop]: { flex: 1, minHeight: 0, overflowY: "auto", overscrollBehavior: "contain", scrollbarGutter: "stable" } } });
globalStyle(".agent-stage > .workflow-detail-content", { "@media": { [desktop]: { overscrollBehavior: "contain" } } });

const shortDesktop = "screen and (min-width: 1101px) and (max-height: 650px)";
globalStyle(".object-list-page.workspace-page, .voice-agent-create-page.workspace-page", { "@media": { [shortDesktop]: { paddingTop: 24, paddingBottom: 16 } } });
globalStyle(".voice-agent-create-page .voice-agent-create-card > .panel-heading", { "@media": { [shortDesktop]: { paddingTop: 12 } } });
globalStyle(".voice-agent-create-page .voice-agent-create-card > .form-grid", { "@media": { [shortDesktop]: { padding: "16px 0" } } });
globalStyle(".agent-workbench .agent-sidebar-content", { "@media": { [shortDesktop]: { paddingTop: 16, paddingBottom: 12 } } });
globalStyle(".agent-workbench .agent-object-context", { "@media": { [shortDesktop]: { maxHeight: "min(40dvh, max(96px, calc(100dvh - 420px)))" } } });
globalStyle(".agent-workbench .builder-back-link", { "@media": { [shortDesktop]: { marginBottom: 16 } } });
globalStyle(".agent-sidebar-content > .agent-node-library", { "@media": { [shortDesktop]: { marginTop: 16 } } });
