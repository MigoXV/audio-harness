import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { AudioHarnessApi, AudioWorkflowDocument, VoiceAgentDefinition } from "./api/types";

const document: AudioWorkflowDocument = {
  schema_version: 1,
  nodes: [
    { id: "physical-vad", kind: "physical_vad", engine_id: "vad-1", config: { min_segment_seconds: .5, max_segment_seconds: 60, audio_buffer_seconds: 120, speech_pre_roll_seconds: .3 } },
    { id: "asr", kind: "asr", engine_id: "asr-1", config: {} },
  ],
  edges: [
    { id: "audio-vad", source: { kind: "input", name: "audio" }, target: { node_id: "physical-vad", port: "audio" } },
    { id: "vad-asr", source: { kind: "node", node_id: "physical-vad", port: "segments" }, target: { node_id: "asr", port: "segments" } },
  ],
  outputs: [
    { name: "primary_transcript", source: { kind: "node", node_id: "asr", port: "transcripts" } },
    { name: "speech_started", source: { kind: "node", node_id: "physical-vad", port: "speech_started" } },
    { name: "speech_stopped", source: { kind: "node", node_id: "physical-vad", port: "speech_stopped" } },
  ],
  layout: { "physical-vad": { x: 240, y: 120 }, asr: { x: 520, y: 120 } },
};

function fakeApi() {
  const agentDefinition: VoiceAgentDefinition = {
    schema_version: 1,
    audio_workflow_id: "wf-1",
    responses_engine_id: "llm-1",
    responses: { model: "deepseek-chat", thinking_enabled: false, instructions: "简洁回答", max_output_tokens: 1024, max_tool_rounds: 8, max_parallel_tools: 4, tool_timeout_seconds: 30, service_tools: [], mcp_servers: [], history_max_items: 128 },
    tts_engine_id: "tts-1",
    tts: { default_voice: "Cherry", allowed_voices: ["Cherry"], language: "Auto", decoder_chunk_size: 0 },
  };
  const agent = { id: "agent-1", slug: "voice-helper", name: "语音助手", description: null, draft_revision: 3, active_revision: 1, created_at: "2026-08-09T00:00:00Z", updated_at: "2026-08-09T00:00:00Z" };
  const createAudioWorkflow = vi.fn().mockResolvedValue({ id: "wf-new", slug: "new-audio", name: "New", description: null, draft_revision: 1, active_revision: null, created_at: "2026-08-09T00:00:00Z", updated_at: "2026-08-09T00:00:00Z" });
  const saveAudioWorkflowDraft = vi.fn().mockImplementation(async (_id, next, revision) => ({ id: "wf-1", slug: "audio-main", name: "主语音图", description: null, document: next, revision: revision + 1, active_revision: 1, updated_at: "2026-08-09T00:00:00Z" }));
  const saveVoiceAgentDraft = vi.fn().mockImplementation(async (_id, next, revision) => ({ ...agent, definition: next, revision: revision + 1 }));
  const engine = (id: string, kind: "vad" | "asr" | "llm" | "tts", api: "ux_vad" | "ux_speech" | "responses" | "qwen_tts_duplex") => ({
    id, slug: id, name: id.toUpperCase(), kind, api, protocol: "grpc", target: "test:1", connect_timeout_seconds: 1,
    request_timeout_seconds: null, credential_env: null, api_key: null, enabled: true, status: "healthy", image: null,
    description: null, status_basis: "人工连通性校验通过", status_observed_at: "2026-08-09T00:00:00Z", last_success_at: null,
    probe_latency_ms: 2, consecutive_failures: 0, next_probe_at: null, source: null, details: {}, revision: 1,
    created_at: "2026-08-09T00:00:00Z", updated_at: "2026-08-09T00:00:00Z",
  });
  const api = {
    getRuntimeOverview: vi.fn().mockResolvedValue({
      status: "ready", ready: true, control_plane: "ready", problems: [], issues: [],
      grpc: { status: "ready", workflow_id: "wf-1", workflow_revision: 1 },
      realtime: { status: "ready", public_routes: 1, internal_routes: 0, invalid_routes: 0 },
    }),
    listAudioWorkflows: vi.fn().mockResolvedValue([{ id: "wf-1", slug: "audio-main", name: "主语音图", description: null, draft_revision: 2, active_revision: 1, created_at: "2026-08-09T00:00:00Z", updated_at: "2026-08-09T00:00:00Z" }]),
    createAudioWorkflow,
    getAudioWorkflowDraft: vi.fn().mockResolvedValue({ id: "wf-1", slug: "audio-main", name: "主语音图", description: null, document, revision: 2, active_revision: 1, updated_at: "2026-08-09T00:00:00Z" }),
    saveAudioWorkflowDraft,
    publishAudioWorkflow: vi.fn().mockResolvedValue({ workflow_id: "wf-1", revision: 2, document, engine_revisions: {}, activated_at: "2026-08-09T00:00:00Z" }),
    validateAudioWorkflow: vi.fn().mockResolvedValue({ valid: true, diagnostics: [], topological_order: ["physical-vad", "asr"] }),
    listAudioNodeTypes: vi.fn().mockResolvedValue([
      { kind: "physical_vad", display_name: "物理 VAD", required_api: "ux_vad", inputs: [{ name: "audio", kind: "pcm_chunk" }], outputs: [{ name: "segments", kind: "audio_segment" }, { name: "speech_started", kind: "speech_started" }, { name: "speech_stopped", kind: "speech_stopped" }], config_schema: {} },
      { kind: "semantic_vad", display_name: "语义 VAD", required_api: "ux_vad", inputs: [{ name: "segments", kind: "audio_segment" }], outputs: [{ name: "segments", kind: "audio_segment" }], config_schema: {} },
      { kind: "asr", display_name: "语音识别", required_api: "ux_speech", inputs: [{ name: "segments", kind: "audio_segment" }], outputs: [{ name: "transcripts", kind: "transcription" }], config_schema: {} },
    ]),
    listAudioWorkflowRevisions: vi.fn().mockResolvedValue([]),
    listEngines: vi.fn().mockResolvedValue({ items: [engine("vad-1", "vad", "ux_vad"), engine("asr-1", "asr", "ux_speech"), engine("llm-1", "llm", "responses"), engine("tts-1", "tts", "qwen_tts_duplex")], total: 4, limit: 200, offset: 0 }),
    listEngineModels: vi.fn().mockResolvedValue({ engine_id: "llm-1", engine_revision: 1, items: ["deepseek-chat", "deepseek-reasoner"] }),
    getRuntimeStatus: vi.fn().mockResolvedValue({ grpc: { active: 1, capacity: 64, rejected: 0 }, realtime: { active: 2, capacity: 64, rejected: 1 }, webrtc: { active: 1, capacity: 64, rejected: 0 }, asr: { active: 2, capacity: 8, rejected: 0 }, responses: { active: 1, capacity: 8, rejected: 0 }, tts: { active: 1, capacity: 1, rejected: 2 }, started_at: "2026-08-09T00:00:00Z" }),
    getCapacitySettings: vi.fn().mockResolvedValue({ grpc: { max_sessions: 64 }, realtime: { max_sessions: 64 }, webrtc: { max_sessions: 64 }, max_asr_concurrency: 8, max_responses_concurrency: 8, max_tts_concurrency: 1, revision: 1, updated_at: "2026-08-09T00:00:00Z" }),
    getWorkflowSettings: vi.fn().mockResolvedValue({ min_chunk_bytes: 640, default_language_code: "zh-CN", audio_queue_capacity: 32, segment_queue_capacity: 8, result_queue_capacity: 32, grpc_audio_workflow_id: "wf-1", revision: 1, updated_at: "2026-08-09T00:00:00Z" }),
    listVoiceAgents: vi.fn().mockResolvedValue([agent]),
    getVoiceAgentDraft: vi.fn().mockResolvedValue({ ...agent, definition: agentDefinition, revision: 3 }),
    saveVoiceAgentDraft,
    validateVoiceAgent: vi.fn().mockResolvedValue({ valid: true, diagnostics: [] }),
    publishVoiceAgent: vi.fn().mockResolvedValue({ ...agent, definition: agentDefinition, revision: 2, audio_workflow_revision: 1, engine_revisions: {} }),
    listVoiceAgentRevisions: vi.fn().mockResolvedValue([]),
    restoreVoiceAgentRevision: vi.fn(),
    createVoiceAgent: vi.fn(),
    listRealtimeRoutes: vi.fn().mockResolvedValue([{ model: "voice-helper", target_type: "voice_agent", target_id: "agent-1", visibility: "public", created_at: "2026-08-09T00:00:00Z", updated_at: "2026-08-09T00:00:00Z" }]),
    updateCapacitySettings: vi.fn(), updateWorkflowSettings: vi.fn(), createEngine: vi.fn(), updateEngine: vi.fn(), deleteEngine: vi.fn(), getEngineUsage: vi.fn().mockResolvedValue({ engine_id: "vad-1", references: [] }), probeEngine: vi.fn(),
  } as unknown as AudioHarnessApi;
  return { api, createAudioWorkflow, saveAudioWorkflowDraft, saveVoiceAgentDraft };
}

function renderApp(api: AudioHarnessApi, path = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App api={api} /></MemoryRouter></QueryClientProvider>);
}

describe("typed audio workflow control plane", () => {
  it("uses audio workflows as the product home", async () => {
    const { api } = fakeApi(); renderApp(api);
    expect(await screen.findByRole("heading", { name: "语音推理工作流" })).toBeVisible();
    expect(await screen.findByText("主语音图")).toBeVisible();
  });

  it("creates a standalone audio workflow", async () => {
    const { api, createAudioWorkflow } = fakeApi(); renderApp(api, "/audio-workflows");
    fireEvent.click(await screen.findByRole("button", { name: "新建语音工作流" }));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "新语音图" } });
    fireEvent.change(screen.getAllByRole("textbox")[1], { target: { value: "new-audio" } });
    fireEvent.click(screen.getByRole("button", { name: "创建并编辑" }));
    await waitFor(() => expect(createAudioWorkflow).toHaveBeenCalledWith(expect.objectContaining({ slug: "new-audio" })));
  });

  it("renders a real port-based DAG editor", async () => {
    const { api } = fakeApi(); renderApp(api, "/audio-workflows/wf-1/build");
    expect(await screen.findByRole("heading", { name: "节点目录" })).toBeVisible();
    const templateSection = screen.getByRole("heading", { name: "快速模板" }).closest("details");
    const nodeSection = screen.getByRole("heading", { name: "节点目录" }).closest("details");
    expect(templateSection).not.toHaveAttribute("open");
    expect(nodeSection).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("快速模板"));
    fireEvent.click(screen.getByText("节点目录"));
    expect(templateSection).toHaveAttribute("open");
    expect(nodeSection).toHaveAttribute("open");
    expect(screen.getByRole("region", { name: "语音工作流画布" })).toBeVisible();
    expect(screen.getByRole("navigation", { name: "工作流页面" })).toBeVisible();
    expect(screen.getByRole("button", { name: /^语义 VAD/ })).toBeVisible();

    const inputNode = screen.getByLabelText("节点 PCM 输入");
    const processorNode = screen.getByLabelText("节点 physical-vad");
    const outputNode = screen.getByLabelText("节点 命名出口");
    expect(inputNode).toHaveAttribute("data-node-role", "input");
    expect(processorNode).toHaveAttribute("data-node-role", "processor");
    expect(processorNode).toHaveAttribute("data-node-state", "configured");
    expect(outputNode).toHaveAttribute("data-node-role", "output");

    fireEvent.click(screen.getByRole("button", { name: "收起工作流侧栏" }));
    expect(screen.getByRole("button", { name: "展开工作流侧栏" })).toBeVisible();
    expect(screen.queryByRole("navigation", { name: "工作流页面" })).not.toBeInTheDocument();

    fireEvent.click(processorNode);
    expect(await screen.findByLabelText("节点配置面板")).toBeVisible();
    expect(screen.getByDisplayValue("physical-vad")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "关闭节点配置" }));
    expect(screen.queryByLabelText("节点配置面板")).not.toBeInTheDocument();
  });

  it("keeps one left sidebar across workflow pages", async () => {
    const { api } = fakeApi(); renderApp(api, "/audio-workflows/wf-1/build");
    expect(await screen.findByLabelText("工作流侧栏")).toBeVisible();
    expect(screen.getByRole("navigation", { name: "工作流页面" })).toBeVisible();

    expect(screen.queryByRole("link", { name: "概览" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "发布" })).not.toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("navigation", { name: "工作流页面" })).getByRole("link", { name: /接入/ }));
    expect(await screen.findByRole("heading", { name: "发布当前草稿" })).toBeVisible();
    expect(screen.getByRole("button", { name: "编译并发布" })).toBeVisible();
    expect(screen.getByLabelText("工作流侧栏")).toBeVisible();
    expect(screen.getByRole("navigation", { name: "工作流页面" })).toBeVisible();

    fireEvent.click(screen.getByRole("link", { name: /测试/ }));
    expect(await screen.findByRole("heading", { name: "配置公开路由后开始测试" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "编译并发布" })).not.toBeInTheDocument();
  });

  it("redirects the legacy workflow publish route to access", async () => {
    const { api } = fakeApi(); renderApp(api, "/audio-workflows/wf-1/publish");
    expect(await screen.findByRole("heading", { name: "发布当前草稿" })).toBeVisible();
    expect(within(screen.getByRole("navigation", { name: "工作流页面" })).getByRole("link", { name: /接入/ })).toHaveAttribute("aria-current", "page");
  });

  it("uses the shared workbench for voice intelligence pages", async () => {
    const { api, saveVoiceAgentDraft } = fakeApi(); renderApp(api, "/voice-agents/agent-1/configure");
    expect(await screen.findByLabelText("语音智能体侧栏")).toBeVisible();
    expect(screen.getByRole("navigation", { name: "语音智能体页面" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "语音智能体配置" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "概览" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "发布" })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Responses 模型"), { target: { value: "new-model" } });
    const saveButton = screen.getByRole("button", { name: "保存草稿" });
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);
    await waitFor(() => expect(saveVoiceAgentDraft).toHaveBeenCalled());

    fireEvent.click(within(screen.getByRole("navigation", { name: "语音智能体页面" })).getByRole("link", { name: /接入/ }));
    expect(await screen.findByRole("heading", { name: "发布当前草稿" })).toBeVisible();
    expect(screen.getByRole("button", { name: "编译并发布" })).toBeVisible();
    expect(screen.getByLabelText("语音智能体侧栏")).toBeVisible();

    fireEvent.click(screen.getByRole("link", { name: /测试/ }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "编译并发布" })).not.toBeInTheDocument());
    const ahaToggle = await screen.findByRole("checkbox", { name: "启用 AHA 扩展协议" });
    expect(screen.queryByLabelText("测试 model")).not.toBeInTheDocument();
    expect(ahaToggle).toBeChecked();
    fireEvent.click(ahaToggle);
    expect(ahaToggle).not.toBeChecked();
  });

  it("redirects the legacy voice agent publish route to access", async () => {
    const { api } = fakeApi(); renderApp(api, "/voice-agents/agent-1/publish");
    expect(await screen.findByRole("heading", { name: "发布当前草稿" })).toBeVisible();
    expect(within(screen.getByRole("navigation", { name: "语音智能体页面" })).getByRole("link", { name: /接入/ })).toHaveAttribute("aria-current", "page");
  });

  it("loads Responses model choices from the selected engine", async () => {
    const { api } = fakeApi(); renderApp(api, "/voice-agents/agent-1/configure");
    expect(await screen.findByText("已从 LLM-1 获取 2 个模型。")).toBeVisible();
    expect(api.listEngineModels).toHaveBeenCalledWith("llm-1", expect.any(AbortSignal));
    const modelInput = screen.getByRole("combobox", { name: "Responses 模型" });
    fireEvent.click(modelInput);
    expect(screen.getByRole("listbox", { name: "可用 Responses 模型" })).toBeVisible();
    expect(screen.getByRole("option", { name: "deepseek-reasoner" })).toBeVisible();
  });

  it("moves voice intelligence creation steps into the sidebar", async () => {
    const { api } = fakeApi(); renderApp(api, "/voice-agents/new");
    expect(await screen.findByLabelText("语音智能体创建侧栏")).toBeVisible();
    expect(screen.getByRole("list", { name: "创建步骤" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "给语音智能体一个清晰身份" })).toBeVisible();
    expect(screen.getByRole("button", { name: /语音工作流/ })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "客服助手" } });
    fireEvent.change(screen.getAllByRole("textbox")[1], { target: { value: "support-agent" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    expect(await screen.findByRole("heading", { name: "选择语音处理基础" })).toBeVisible();
    expect(screen.getByRole("button", { name: /基本信息/ })).toBeEnabled();
  });

  it("persists an explicit automatic graph layout", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi(); renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByRole("button", { name: "自动排列" }));

    await waitFor(() => expect(saveAudioWorkflowDraft).toHaveBeenCalled(), { timeout: 2_000 });
    const savedDocument = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
    const physicalVadPosition = savedDocument.layout?.["physical-vad"];
    const asrPosition = savedDocument.layout?.asr;
    expect(physicalVadPosition).toBeDefined();
    expect(asrPosition).toBeDefined();
    expect(physicalVadPosition!.x).toBeLessThan(asrPosition!.x);
  });

  it("allows an audio workflow draft to be saved manually", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi(); renderApp(api, "/audio-workflows/wf-1/build");
    const saveButton = await screen.findByRole("button", { name: "保存草稿" });
    expect(saveButton).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /物理 \+ 语义 VAD \+ ASR/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);

    await waitFor(() => expect(saveAudioWorkflowDraft).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(saveButton).toBeDisabled());
  });

  it("applies a complete semantic VAD template and preserves engine choices", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi(); renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByRole("button", { name: /物理 \+ 语义 VAD \+ ASR/ }));
    expect(screen.getByRole("alertdialog", { name: "应用快速模板？" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));

    await waitFor(() => expect(saveAudioWorkflowDraft).toHaveBeenCalled(), { timeout: 2_000 });
    const savedDocument = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
    expect(savedDocument.nodes.map((node) => node.kind)).toEqual(["physical_vad", "semantic_vad", "asr"]);
    expect(savedDocument.nodes.find((node) => node.kind === "physical_vad")?.engine_id).toBe("vad-1");
    expect(savedDocument.nodes.find((node) => node.kind === "asr")?.engine_id).toBe("asr-1");
    expect(savedDocument.outputs.map((output) => output.name)).toContain("turn_progress");
  });

  it("shows live runtime capacity", async () => {
    const { api } = fakeApi(); renderApp(api, "/runtime");
    expect(await screen.findByRole("heading", { name: "接入与运行" })).toBeVisible();
    expect(screen.getByText(/累计拒绝 2/)).toBeVisible();
  });

  it("uses the generated engine contract", async () => {
    const { api } = fakeApi(); renderApp(api, "/engines");
    expect(await screen.findByRole("heading", { name: "推理引擎" })).toBeVisible();
    expect(screen.queryByText("人工连通性校验通过")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /VAD-1/ }));
    fireEvent.click(screen.getByRole("button", { name: /ASR-1/ }));
    expect(await screen.findAllByText("人工连通性校验通过")).toHaveLength(2);
  });

  it("provides safe engine lifecycle actions", async () => {
    const { api } = fakeApi(); renderApp(api, "/engines");
    await screen.findByRole("heading", { name: "推理引擎" });
    fireEvent.click((await screen.findAllByRole("button", { name: "停用" }))[0]);
    expect(await screen.findByRole("alertdialog", { name: "停用引擎？" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "确认停用" }));
    await waitFor(() => expect(api.updateEngine).toHaveBeenCalledWith("vad-1", { enabled: false }));
  });

  it("re-enables a disabled engine from its collapsed card", async () => {
    const { api } = fakeApi();
    const response = await api.listEngines();
    const disabled = { ...response.items[0], enabled: false, status: "disabled" as const };
    api.listEngines = vi.fn().mockResolvedValue({ ...response, items: [disabled, ...response.items.slice(1)] });
    renderApp(api, "/engines");
    fireEvent.click(await screen.findByRole("button", { name: "启用" }));
    await waitFor(() => expect(api.updateEngine).toHaveBeenCalledWith("vad-1", { enabled: true }));
  });

  it("explains references that block engine disable", async () => {
    const { api } = fakeApi();
    api.getEngineUsage = vi.fn().mockResolvedValue({ engine_id: "vad-1", references: ["主语音图 草稿"] });
    renderApp(api, "/engines");
    await screen.findByRole("heading", { name: "推理引擎" });
    fireEvent.click((await screen.findAllByRole("button", { name: "停用" }))[0]);
    expect(await screen.findByRole("alertdialog", { name: "当前不能停用引擎" })).toBeVisible();
    expect(screen.getByText("主语音图 草稿")).toBeVisible();
    expect(screen.queryByRole("button", { name: "确认停用" })).not.toBeInTheDocument();
  });

  it("distinguishes the initial engine load from an empty catalog", async () => {
    const { api } = fakeApi();
    let resolveEngines!: (value: Awaited<ReturnType<AudioHarnessApi["listEngines"]>>) => void;
    api.listEngines = vi.fn().mockReturnValue(new Promise((resolve) => { resolveEngines = resolve; }));
    renderApp(api, "/engines");

    expect(await screen.findByRole("status")).toHaveTextContent("正在加载引擎");
    expect(screen.queryByText("没有符合条件的引擎")).not.toBeInTheDocument();

    resolveEngines({ items: [], total: 0, limit: 200, offset: 0 });
    expect(await screen.findByText("尚未注册引擎")).toBeVisible();
  });
});
