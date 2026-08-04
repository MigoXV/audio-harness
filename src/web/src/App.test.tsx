import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { AudioHarnessApi, StageWorkflowDefinition } from "./api/types";

const definition: StageWorkflowDefinition = {
  schema_version: 2,
  stages: {
    physical_vad: { engine_id: "vad-1", config: { min_segment_seconds: .5, max_segment_seconds: 60, audio_buffer_seconds: 120, speech_pre_roll_seconds: .3 } },
    semantic_vad: null,
    asr: { engine_id: "asr-1", config: {} },
    responses_agent: { engine_id: "llm-1", config: { model: "model-a", thinking_enabled: false, instructions: "", max_output_tokens: 1024, max_tool_rounds: 8, max_parallel_tools: 4, tool_timeout_seconds: 30, service_tools: [], mcp_servers: [], history_max_items: 128 } },
    duplex_tts: { engine_id: "tts-1", config: { default_voice: "voice-a", allowed_voices: ["voice-a"], language: "Auto", decoder_chunk_size: 0 } },
  },
};

function fakeApi() {
  const saveManagedWorkflowDraft = vi.fn().mockImplementation(async (_id, next, revision) => ({ workflow_id: "wf-1", definition: next, revision: revision + 1, updated_at: "2026-08-04T00:00:00Z", active_revision: 1 }));
  const createManagedWorkflow = vi.fn().mockResolvedValue({ workflow_id: "wf-new", definition, revision: 1, updated_at: "2026-08-04T00:00:00Z", active_revision: null });
  const api = {
    getReadiness: vi.fn().mockResolvedValue({ status: "ready", database: "ready", data_plane: "ready", problems: [], pipeline_revision: 1, nodes: [] }),
    listWorkflows: vi.fn().mockResolvedValue([{ id: "wf-1", slug: "voice-agent", name: "客服 Agent", kind: "voice_agent", description: null, draft_revision: 2, active_revision: 1, created_at: "2026-08-04T00:00:00Z", updated_at: "2026-08-04T00:00:00Z" }]),
    getWorkflowStatus: vi.fn().mockResolvedValue({ workflow_id: "wf-1", state: "published_healthy", draft_revision: 2, active_revision: 1, has_unpublished_changes: false, public_models: ["voice-agent"], diagnostics: [], stages: [
      { stage: "physical_vad", engine_id: "vad-1", engine_name: "VAD", health: "healthy", reason: null, checked_at: "2026-08-04T00:00:00Z" },
      { stage: "asr", engine_id: "asr-1", engine_name: "ASR", health: "healthy", reason: null, checked_at: "2026-08-04T00:00:00Z" },
      { stage: "responses_agent", engine_id: "llm-1", engine_name: "LLM", health: "healthy", reason: null, checked_at: "2026-08-04T00:00:00Z" },
      { stage: "duplex_tts", engine_id: "tts-1", engine_name: "TTS", health: "healthy", reason: null, checked_at: "2026-08-04T00:00:00Z" },
    ] }),
    getManagedWorkflowDraft: vi.fn().mockResolvedValue({ workflow_id: "wf-1", definition, revision: 2, updated_at: "2026-08-04T00:00:00Z", active_revision: 1 }),
    saveManagedWorkflowDraft,
    createManagedWorkflow,
    listWorkflowRevisions: vi.fn().mockResolvedValue([]),
    listEngines: vi.fn().mockResolvedValue({ items: [
      { id: "vad-1", slug: "vad", name: "VAD", kind: "vad", api: "ux_vad", protocol: "grpc", target: "127.0.0.1:1", connect_timeout_seconds: 10, request_timeout_seconds: null, enabled: true, status: "healthy", image: null, description: null, status_basis: "后台连通性校验通过", status_observed_at: "2026-08-04T00:00:00Z", source: null, details: {}, revision: 1, created_at: "2026-08-04T00:00:00Z", updated_at: "2026-08-04T00:00:00Z" },
      { id: "asr-1", slug: "asr", name: "ASR", kind: "asr", api: "ux_speech", protocol: "grpc", target: "127.0.0.1:2", connect_timeout_seconds: 10, request_timeout_seconds: null, enabled: true, status: "healthy", image: null, description: null, status_basis: null, status_observed_at: null, source: null, details: {}, revision: 1, created_at: "2026-08-04T00:00:00Z", updated_at: "2026-08-04T00:00:00Z" },
    ], total: 2, limit: 200, offset: 0 }),
    getRuntimeStatus: vi.fn().mockResolvedValue({ grpc: { active: 1, capacity: 64, rejected: 0 }, realtime: { active: 2, capacity: 64, rejected: 1 }, webrtc: { active: 1, capacity: 64, rejected: 0 }, asr: { active: 2, capacity: 8, rejected: 0 }, responses: { active: 1, capacity: 8, rejected: 0 }, tts: { active: 1, capacity: 1, rejected: 2 }, started_at: "2026-08-04T00:00:00Z" }),
    getCapacitySettings: vi.fn().mockResolvedValue({ grpc: { max_sessions: 64 }, realtime: { max_sessions: 64 }, webrtc: { max_sessions: 64 }, max_asr_concurrency: 8, max_responses_concurrency: 8, max_tts_concurrency: 1, revision: 1, updated_at: "2026-08-04T00:00:00Z" }),
    getWorkflowSettings: vi.fn().mockResolvedValue({ min_chunk_bytes: 640, default_language_code: "zh-CN", audio_queue_capacity: 32, segment_queue_capacity: 8, result_queue_capacity: 32, revision: 1, updated_at: "2026-08-04T00:00:00Z" }),
    updateCapacitySettings: vi.fn(), updateWorkflowSettings: vi.fn(), createEngine: vi.fn(), updateEngine: vi.fn(), deleteEngine: vi.fn(), listNodeTypes: vi.fn(), getPipeline: vi.fn(), updatePipelineDraft: vi.fn(), validatePipeline: vi.fn(), activatePipeline: vi.fn(), getActivePipeline: vi.fn(),
  } as unknown as AudioHarnessApi;
  return { api, saveManagedWorkflowDraft, createManagedWorkflow };
}

function renderApp(api: AudioHarnessApi, path = "/") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[path]}><App api={api} /></MemoryRouter></QueryClientProvider>);
}

describe("Audio Harness v2 control plane", () => {
  it("uses workflows as the product home", async () => {
    const { api } = fakeApi(); renderApp(api);
    expect(await screen.findByRole("heading", { name: "实时语音工作流" })).toBeVisible();
    expect(await screen.findByText("客服 Agent")).toBeVisible();
    expect(screen.getByText("运行健康")).toBeVisible();
  });

  it("creates a workflow whose public model follows the slug", async () => {
    const { api, createManagedWorkflow } = fakeApi(); renderApp(api, "/workflows");
    fireEvent.click(await screen.findByRole("button", { name: "新建工作流" }));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "新的 Agent" } });
    fireEvent.change(screen.getByPlaceholderText("customer-service-agent"), { target: { value: "new-agent" } });
    fireEvent.click(screen.getByRole("button", { name: "创建并进入构建" }));
    await waitFor(() => expect(createManagedWorkflow).toHaveBeenCalledWith(expect.objectContaining({ slug: "new-agent", kind: "voice_agent" })));
  });

  it("renders the constrained runtime topology and autosaves typed fields", async () => {
    const { api, saveManagedWorkflowDraft } = fakeApi(); renderApp(api, "/workflows/wf-1/build");
    expect(await screen.findByRole("heading", { name: "真实运行拓扑" })).toBeVisible();
    expect(screen.getByText("双向流式 TTS", { selector: ".topology-node strong" })).toBeVisible();
    expect(screen.getByLabelText("启用思考（默认关闭）")).not.toBeChecked();
    fireEvent.click(screen.getByLabelText("启用思考（默认关闭）"));
    fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "model-b" } });
    await waitFor(() => expect(saveManagedWorkflowDraft).toHaveBeenCalled(), { timeout: 1800 });
    expect(saveManagedWorkflowDraft).toHaveBeenLastCalledWith(
      "wf-1",
      expect.objectContaining({
        stages: expect.objectContaining({
          responses_agent: expect.objectContaining({
            config: expect.objectContaining({ thinking_enabled: true }),
          }),
        }),
      }),
      2,
    );
  });

  it("shows WSS and WebRTC in the built-in test console", async () => {
    const { api } = fakeApi(); renderApp(api, "/workflows/wf-1/test");
    expect(await screen.findByRole("heading", { name: "端到端实时测试" })).toBeVisible();
    expect(screen.getByRole("button", { name: "WebRTC" })).toBeVisible();
    expect(screen.getByRole("button", { name: "WSS" })).toBeVisible();
  });

  it("reviews the release snapshot before publishing", async () => {
    const { api } = fakeApi(); renderApp(api, "/workflows/wf-1/publish");
    expect(await screen.findByRole("heading", { name: "重新发布当前草稿" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "运行阶段快照" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "检查并发布" }));
    expect(await screen.findByRole("alertdialog", { name: "确认重新发布当前草稿？" })).toBeVisible();
    expect(screen.getByText("若检查或激活失败，当前运行版本不会改变。", { exact: false })).toBeVisible();
  });

  it("shows live capacity and rejects", async () => {
    const { api } = fakeApi(); renderApp(api, "/runtime");
    expect(await screen.findByRole("heading", { name: "运行状态与容量" })).toBeVisible();
    expect(screen.getByText(/累计拒绝 2/)).toBeVisible();
    expect(screen.getByText("TTS 可能成为容量瓶颈")).toBeVisible();
  });

  it("uses the new engine console", async () => {
    const { api } = fakeApi(); renderApp(api, "/engines");
    expect(await screen.findByRole("heading", { name: "推理引擎" })).toBeVisible();
    expect(await screen.findByText("后台连通性校验通过")).toBeVisible();
  });

  it("confirms an engine deletion in an accessible dialog", async () => {
    const { api } = fakeApi(); renderApp(api, "/engines");
    await screen.findByRole("heading", { name: "推理引擎" });
    fireEvent.click((await screen.findAllByRole("button", { name: "删除" }))[0]);
    expect(await screen.findByRole("alertdialog", { name: "删除引擎？" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("has a safe unknown route", () => {
    const { api } = fakeApi(); renderApp(api, "/missing");
    expect(screen.getByRole("link", { name: "返回工作流" })).toBeVisible();
  });
});
