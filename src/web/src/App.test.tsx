import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { AudioHarnessApi, AudioWorkflowDocument, VoiceAgentDefinition } from "./api/types";

const document: AudioWorkflowDocument = {
  schema_version: 1,
  nodes: [
    { id: "physical-vad", kind: "physical_vad", engine_id: "vad-1", config: { min_segment_seconds: .5, max_segment_seconds: 60, audio_buffer_seconds: 120, speech_pre_roll_seconds: .3, progress_interval_seconds: .2 } },
    { id: "asr", kind: "asr", engine_id: "asr-1", config: { remove_punctuation: false } },
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
  const createVoiceAgent = vi.fn().mockResolvedValue(agent);
  const engine = (id: string, kind: "denoise" | "vad" | "asr" | "llm" | "tts" | "embedding", api: "ux_denoise" | "ux_vad" | "ux_speech" | "responses" | "qwen_tts_duplex" | "engine_feature") => ({
    id, slug: id, name: id.toUpperCase(), kind, api, protocol: "grpc", target: "test:1", connect_timeout_seconds: 1,
    request_timeout_seconds: null, credential_env: null, api_key: null, enabled: true, status: "healthy", image: null,
    description: null, status_basis: "人工连通性校验通过", status_observed_at: "2026-08-09T00:00:00Z", last_success_at: null,
    probe_latency_ms: 2, consecutive_failures: 0, next_probe_at: null, source: null, details: {}, revision: 1,
    created_at: "2026-08-09T00:00:00Z", updated_at: "2026-08-09T00:00:00Z",
  });
  const api = {
    getRuntimeOverview: vi.fn().mockResolvedValue({
      status: "ready", control_plane: "ready", issues: [],
      realtime: { state: "ready", public_routes: 1, internal_routes: 0 },
    }),
    listAudioWorkflows: vi.fn().mockResolvedValue([{ id: "wf-1", slug: "audio-main", name: "主语音图", description: null, draft_revision: 2, active_revision: 1, created_at: "2026-08-09T00:00:00Z", updated_at: "2026-08-09T00:00:00Z" }]),
    createAudioWorkflow,
    getAudioWorkflowDraft: vi.fn().mockResolvedValue({ id: "wf-1", slug: "audio-main", name: "主语音图", description: null, document, revision: 2, active_revision: 1, updated_at: "2026-08-09T00:00:00Z" }),
    getActiveAudioWorkflow: vi.fn().mockResolvedValue({
      workflow_id: "wf-1",
      revision: 1,
      document: { ...document, outputs: [...document.outputs, { name: "speaker_filter", source: { kind: "node", node_id: "physical-vad", port: "speech_started" } }] },
      engine_revisions: { "vad-1": 1, "asr-1": 1 },
      activated_at: "2026-08-09T00:00:00Z",
    }),
    saveAudioWorkflowDraft,
    publishAudioWorkflow: vi.fn().mockResolvedValue({ workflow_id: "wf-1", revision: 2, document, engine_revisions: {}, activated_at: "2026-08-09T00:00:00Z" }),
    validateAudioWorkflow: vi.fn().mockResolvedValue({ valid: true, diagnostics: [], topological_order: ["physical-vad", "asr"] }),
    listAudioNodeTypes: vi.fn().mockResolvedValue([
      { kind: "automatic_gain_control", display_name: "自动增益控制", required_api: null, inputs: [{ name: "audio", kind: "pcm_chunk" }], outputs: [{ name: "audio", kind: "pcm_chunk" }], config_schema: {} },
      { kind: "streaming_denoise", display_name: "在线降噪", required_api: "ux_denoise", inputs: [{ name: "audio", kind: "pcm_chunk" }], outputs: [{ name: "audio", kind: "vad_audio_chunk" }], config_schema: {} },
      { kind: "physical_vad", display_name: "物理 VAD", required_api: "ux_vad", inputs: [{ name: "audio", kind: "pcm_chunk" }, { name: "detection_audio", kind: "vad_audio_chunk" }], outputs: [{ name: "segments", kind: "audio_segment" }, { name: "speech_started", kind: "speech_started" }, { name: "speech_stopped", kind: "speech_stopped" }], input_groups: [{ name: "audio_source", ports: ["audio", "detection_audio"], minimum: 1, maximum: 1 }], config_schema: {} },
      { kind: "semantic_vad", display_name: "语义 VAD", required_api: "ux_vad", inputs: [{ name: "segments", kind: "audio_segment" }], outputs: [{ name: "segments", kind: "audio_segment" }, { name: "speech_stopped", kind: "speech_stopped" }, { name: "turn_progress", kind: "transcription" }], config_schema: {} },
      { kind: "asr", display_name: "语音识别", required_api: "ux_speech", inputs: [{ name: "segments", kind: "audio_segment" }], outputs: [{ name: "transcripts", kind: "transcription" }, { name: "speaker_transcripts", kind: "speaker_transcription" }], config_schema: {} },
      { kind: "audio_embedding", display_name: "语音特征向量提取", required_api: "engine_feature", inputs: [{ name: "segments", kind: "audio_segment" }], outputs: [{ name: "embeddings", kind: "embedding" }], config_schema: {} },
      { kind: "vector_search", display_name: "人物搜索", required_api: null, inputs: [{ name: "embeddings", kind: "embedding" }], outputs: [{ name: "results", kind: "vector_search_result" }], config_schema: {} },
      { kind: "speaker_filter", display_name: "说话人过滤", required_api: null, inputs: [{ name: "segments", kind: "audio_segment" }, { name: "results", kind: "vector_search_result" }], outputs: [{ name: "segments", kind: "audio_segment" }, { name: "speech_started", kind: "speech_started" }, { name: "speech_stopped", kind: "speech_stopped" }, { name: "decisions", kind: "speaker_filter_result" }], config_schema: {} },
      { kind: "speaker_diarization", display_name: "说话人分离", required_api: "ux_speaker_diarization", inputs: [{ name: "segments", kind: "audio_segment" }], outputs: [{ name: "segments", kind: "diarization_segment" }, { name: "local_results", kind: "diarization_local_result" }], config_schema: {} },
      { kind: "speaker_clustering", display_name: "说话人聚类", required_api: "engine_feature", inputs: [{ name: "segments", kind: "diarization_segment" }], outputs: [{ name: "results", kind: "diarization_cluster_result" }], config_schema: {} },
    ]),
    listAudioWorkflowRevisions: vi.fn().mockResolvedValue([]),
    listEngines: vi.fn().mockResolvedValue({ items: [engine("vad-1", "vad", "ux_vad"), engine("asr-1", "asr", "ux_speech"), engine("llm-1", "llm", "responses"), engine("tts-1", "tts", "qwen_tts_duplex"), engine("embedding-1", "embedding", "engine_feature"), engine("denoise-1", "denoise", "ux_denoise")], total: 6, limit: 200, offset: 0 }),
    listEngineModels: vi.fn().mockResolvedValue({ engine_id: "llm-1", engine_revision: 1, items: ["deepseek-chat", "deepseek-reasoner"] }),
    getRuntimeStatus: vi.fn().mockResolvedValue({ realtime: { active: 2, capacity: 64, rejected: 1 }, webrtc: { active: 1, capacity: 64, rejected: 0 }, asr: { active: 2, capacity: 8, rejected: 0 }, responses: { active: 1, capacity: 8, rejected: 0 }, tts: { active: 1, capacity: 1, rejected: 2 }, started_at: "2026-08-09T00:00:00Z" }),
    getCapacitySettings: vi.fn().mockResolvedValue({ realtime: { max_sessions: 64 }, webrtc: { max_sessions: 64 }, max_asr_concurrency: 8, max_responses_concurrency: 8, max_tts_concurrency: 1, revision: 1, updated_at: "2026-08-09T00:00:00Z" }),
    getWorkflowSettings: vi.fn().mockResolvedValue({ min_chunk_bytes: 640, default_language_code: "zh-CN", audio_queue_capacity: 32, segment_queue_capacity: 8, result_queue_capacity: 32, received_audio_cache_enabled: false, received_audio_cache_block_seconds: 5, received_audio_cache_file_seconds: 300, revision: 1, updated_at: "2026-08-09T00:00:00Z" }),
    listVoiceAgents: vi.fn().mockResolvedValue([agent]),
    getVoiceAgentDraft: vi.fn().mockResolvedValue({ ...agent, definition: agentDefinition, revision: 3 }),
    saveVoiceAgentDraft,
    validateVoiceAgent: vi.fn().mockResolvedValue({ valid: true, diagnostics: [] }),
    publishVoiceAgent: vi.fn().mockResolvedValue({ ...agent, definition: agentDefinition, revision: 2, audio_workflow_revision: 1, engine_revisions: {} }),
    listVoiceAgentRevisions: vi.fn().mockResolvedValue([]),
    restoreVoiceAgentRevision: vi.fn(),
    createVoiceAgent,
    listRealtimeRoutes: vi.fn().mockResolvedValue([{ model: "voice-helper", target_type: "voice_agent", target_id: "agent-1", visibility: "public", created_at: "2026-08-09T00:00:00Z", updated_at: "2026-08-09T00:00:00Z" }]),
    getVectorInfrastructure: vi.fn().mockResolvedValue({ configured: true, ready: true, grpc_target: "qdrant:6334", http_url: "http://qdrant:6333" }),
    listVectorSpaces: vi.fn().mockResolvedValue([{ collection_name: "voiceprints", vectors: [{ name: "eres2netv2", size: 192, distance: "cosine" }], template: "voiceprint" }]),
    createVectorSpace: vi.fn().mockResolvedValue({ collection_name: "voiceprints", vectors: [{ name: "eres2netv2", size: 192, distance: "cosine" }], template: "voiceprint" }),
    updateCapacitySettings: vi.fn(), updateWorkflowSettings: vi.fn(), createEngine: vi.fn(), updateEngine: vi.fn(), deleteEngine: vi.fn(), getEngineUsage: vi.fn().mockResolvedValue({ engine_id: "vad-1", references: [] }), probeEngine: vi.fn(),
  } as unknown as AudioHarnessApi;
  return { api, createAudioWorkflow, createVoiceAgent, saveAudioWorkflowDraft, saveVoiceAgentDraft };
}

function renderApp(api: AudioHarnessApi, path = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([{ path: "*", element: <App api={api} /> }], { initialEntries: [path] });
  return render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
}

describe("typed audio workflow control plane", () => {
  it("uses audio workflows as the product home", async () => {
    const { api } = fakeApi(); renderApp(api);
    expect(screen.getByRole("link", { name: "Audio Harness 首页" })).toContainHTML('src="/audio-harness-logo.svg"');
    expect(await screen.findByRole("heading", { name: "语音推理工作流" })).toBeVisible();
    expect(await screen.findByText("主语音图")).toBeVisible();
  });

  it("creates a standalone audio workflow", async () => {
    const { api, createAudioWorkflow } = fakeApi(); renderApp(api, "/audio-workflows");
    fireEvent.click(await screen.findByRole("button", { name: "新建语音工作流" }));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "New Audio" } });
    expect(screen.getByDisplayValue("new-audio")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "创建并编辑" }));
    await waitFor(() => expect(createAudioWorkflow).toHaveBeenCalledWith(expect.objectContaining({ slug: "new-audio" })));
  });

  it("stops syncing a workflow slug after manual editing", async () => {
    const { api } = fakeApi(); renderApp(api, "/audio-workflows");
    fireEvent.click(await screen.findByRole("button", { name: "新建语音工作流" }));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "First Name" } });
    fireEvent.change(screen.getByDisplayValue("first-name"), { target: { value: "custom-id" } });
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "Second Name" } });
    expect(screen.getByDisplayValue("custom-id")).toBeVisible();
  });

  it("creates a collection from the voice feature extraction template", async () => {
    const { api } = fakeApi(); renderApp(api, "/vectors");
    expect(await screen.findByRole("heading", { name: "向量检索" })).toBeVisible();
    expect(await screen.findByText("语音特征向量提取")).toBeVisible();
    expect(screen.queryByText(["声", "纹"].join(""))).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "创建 Collection" }));
    expect(screen.getAllByText("语音特征向量提取").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => expect(api.createVectorSpace).toHaveBeenCalledWith({
      collection_name: "voiceprints",
      vectors: [{ name: "eres2netv2", size: 192, distance: "cosine" }],
      template: "voiceprint",
    }));
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
    expect(screen.getByRole("button", { name: /^双路物理 \+ 语义 VAD \+ ASR/ })).toBeVisible();

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

  it("updates engine configuration colors before the draft is saved", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi();
    const unconfiguredDocument: AudioWorkflowDocument = {
      ...structuredClone(document),
      nodes: document.nodes.map((node) => ({ ...node, engine_id: null })),
    };
    api.getAudioWorkflowDraft = vi.fn().mockResolvedValue({
      id: "wf-1",
      slug: "audio-main",
      name: "主语音图",
      description: null,
      document: unconfiguredDocument,
      revision: 2,
      active_revision: null,
      updated_at: "2026-08-09T00:00:00Z",
    });
    api.validateAudioWorkflow = vi.fn().mockResolvedValue({
      valid: false,
      diagnostics: [
        { code: "missing_engine", message: "物理 VAD尚未绑定引擎", node_id: "physical-vad" },
        { code: "missing_engine", message: "语音识别尚未绑定引擎", node_id: "asr" },
      ],
      topological_order: ["physical-vad", "asr"],
    });
    renderApp(api, "/audio-workflows/wf-1/build");

    const physicalNode = await screen.findByLabelText("节点 physical-vad");
    const asrNode = screen.getByLabelText("节点 asr");
    expect(physicalNode).toHaveAttribute("data-node-state", "unconfigured");
    expect(asrNode).toHaveAttribute("data-node-state", "unconfigured");

    fireEvent.click(physicalNode);
    fireEvent.change(screen.getByRole("combobox", { name: /^执行引擎/ }), { target: { value: "vad-1" } });
    await waitFor(() => expect(physicalNode).toHaveAttribute("data-node-state", "configured"));

    fireEvent.click(screen.getByRole("button", { name: "关闭节点配置" }));
    fireEvent.click(asrNode);
    const engineSelect = screen.getByRole("combobox", { name: /^执行引擎/ });
    fireEvent.change(engineSelect, { target: { value: "asr-1" } });
    await waitFor(() => expect(asrNode).toHaveAttribute("data-node-state", "configured"));

    fireEvent.change(engineSelect, { target: { value: "" } });
    await waitFor(() => expect(asrNode).toHaveAttribute("data-node-state", "unconfigured"));
    expect(saveAudioWorkflowDraft).not.toHaveBeenCalled();
    expect(api.validateAudioWorkflow).toHaveBeenCalledTimes(1);
  });

  it("applies the voice agent template with every connection visible", async () => {
    const { api } = fakeApi();
    const { container } = renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByRole("button", { name: /^语音智能体/ }));
    expect(screen.queryByRole("button", { name: /^向量解锁/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));

    for (const nodeId of ["physical-vad", "semantic-vad", "audio-embedding", "vector-search", "speaker-filter", "asr"]) {
      expect(await screen.findByLabelText(`节点 ${nodeId}`)).toBeVisible();
    }

    const expectedEdgeIds = [
      "edge:audio-to-physical-vad",
      "edge:physical-vad-to-semantic-vad",
      "edge:semantic-vad-to-audio-embedding",
      "edge:audio-embedding-to-vector-search",
      "edge:semantic-vad-to-speaker-filter",
      "edge:vector-search-to-speaker-filter",
      "edge:speaker-filter-to-asr",
      "output:primary_transcript",
      "output:speech_started",
      "output:speech_stopped",
      "output:turn_progress",
      "output:vector_search",
      "output:speaker_filter",
    ];
    await waitFor(() => {
      for (const edgeId of expectedEdgeIds) {
        const edge = container.querySelector(`[data-testid="rf__edge-${edgeId}"]`);
        expect(edge, edgeId).not.toBeNull();
        const paths = edge!.querySelectorAll("path.react-flow__edge-path");
        expect(paths.length, edgeId).toBeGreaterThan(0);
        for (const path of paths) expect(path.getAttribute("d"), edgeId).toMatch(/^M /);
      }
    }, { timeout: 3_000 });
  });

  it("configures FireRed postprocessing on physical VAD nodes", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi();
    renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByLabelText("节点 physical-vad"));

    const toggle = screen.getByRole("checkbox", { name: "自定义 FireRed 后处理" });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);

    expect(screen.getByText("协议版本 v1 · 1 帧 = 10 ms")).toBeVisible();
    expect(screen.getByLabelText(/概率平滑窗口/)).toHaveValue(5);
    expect(screen.getByLabelText(/语音判定阈值/)).toHaveValue(0.5);
    expect(screen.getByLabelText(/语音起点前补/)).toHaveValue(5);
    expect(screen.getByLabelText(/最短连续语音/)).toHaveValue(8);
    expect(screen.getByLabelText(/单段最长语音/)).toHaveValue(2000);
    expect(screen.getByLabelText(/结束所需静音/)).toHaveValue(20);

    expect(saveAudioWorkflowDraft).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAudioWorkflowDraft).toHaveBeenCalled());
    const saved = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
    const physical = saved.nodes.find((node) => node.kind === "physical_vad");
    expect(physical?.config?.postprocessing).toEqual({
      processor: "firered.stream_vad",
      version: 1,
      parameters: {
        smooth_window_size: 5,
        speech_threshold: 0.5,
        pad_start_frame: 5,
        min_speech_frame: 8,
        max_speech_frame: 2000,
        min_silence_frame: 20,
      },
    });
  });

  it("configures optional denoise weighting with fail-open default", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi();
    renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByText("节点目录"));
    fireEvent.click(screen.getByRole("button", { name: /^在线降噪/ }));
    fireEvent.change(screen.getByRole("combobox", { name: /执行引擎/ }), { target: { value: "denoise-1" } });

    const toggle = screen.getByRole("checkbox", { name: /混合原音与降噪音频/ });
    expect(screen.queryByText("denoised_weight")).not.toBeInTheDocument();
    expect(toggle).not.toBeChecked();
    expect(screen.getByRole("combobox", { name: "失败策略" })).toHaveValue("fail_open");

    fireEvent.click(toggle);
    expect(screen.getByLabelText(/降噪音频权重/)).toHaveValue(0.5);
    fireEvent.change(screen.getByLabelText(/降噪音频权重/), { target: { value: "0.7" } });

    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAudioWorkflowDraft).toHaveBeenCalled());
    const saved = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
    const denoise = saved.nodes.find((node) => node.kind === "streaming_denoise");
    expect(denoise).toMatchObject({
      engine_id: "denoise-1",
      config: { denoised_weight: 0.7 },
      failure_policy: "fail_open",
    });
  });

  it("configures a transparent automatic gain control node", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi();
    renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByText("节点目录"));
    fireEvent.click(screen.getByRole("button", { name: /^自动增益控制/ }));

    expect(screen.getByText("处理后的 PCM 供全部下游使用")).toBeVisible();
    expect(screen.getByLabelText(/压制响应/)).toHaveValue(0);
    expect(screen.getByLabelText(/增益恢复/)).toHaveValue(2.5);
    expect(screen.getByLabelText(/软拐点宽度/)).toHaveValue(6);
    expect(screen.getByLabelText(/初始检测电平/)).toHaveValue(-12);
    fireEvent.change(screen.getByLabelText(/末级补偿/), { target: { value: "6" } });

    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAudioWorkflowDraft).toHaveBeenCalled());
    const saved = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
    expect(saved.nodes.find((node) => node.kind === "automatic_gain_control")).toMatchObject({
      engine_id: null,
      config: {
        attack_seconds: 0,
        decay_seconds: 2.5,
        soft_knee_db: 6,
        initial_level_db: -12,
        output_gain_db: 6,
      },
    });
  });

  it("configures VAD coverage postprocessing on diarization nodes", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi();
    renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByRole("button", { name: /^会议系统完全体/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));
    fireEvent.click(await screen.findByLabelText("节点 speaker-diarization"));

    const toggle = screen.getByRole("checkbox", { name: /^启用 VAD 覆盖后处理/ });
    expect(toggle).toBeChecked();
    expect(screen.getByText("协议版本 v1 · 稳定片段流式提交")).toBeVisible();

    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => {
      const saved = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
      expect(saved.nodes.find((node) => node.kind === "speaker_diarization")?.config?.postprocessing).toBeNull();
    });

    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => {
      const saved = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
      expect(saved.nodes.find((node) => node.kind === "speaker_diarization")?.config?.postprocessing).toEqual({
        processor: "audio_harness.diarization.vad_coverage",
        version: 1,
      });
    });
  });

  it("configures punctuation removal on ASR nodes", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi();
    renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByLabelText("节点 asr"));

    const toggle = screen.getByRole("checkbox", { name: "移除转写标点" });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);

    expect(saveAudioWorkflowDraft).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAudioWorkflowDraft).toHaveBeenCalled());
    const saved = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
    const asr = saved.nodes.find((node) => node.kind === "asr");
    expect(asr?.config?.remove_punctuation).toBe(true);
  });

  it("configures HDBSCAN speaker clustering with a fixed parameter template", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi();
    renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByRole("button", { name: /^会议系统完全体/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));
    fireEvent.click(await screen.findByLabelText("节点 speaker-clustering"));

    const algorithm = screen.getByRole("combobox", { name: "聚类算法" }) as HTMLSelectElement;
    expect(algorithm).toHaveValue("hdbscan");
    expect(algorithm.options).toHaveLength(1);
    expect(screen.getByText("HDBSCAN 参数模板")).toBeVisible();
    expect(screen.getByLabelText(/最小簇大小/)).toHaveValue(2);
    expect(screen.getByLabelText(/最小样本数/)).toHaveValue(2);
    expect(screen.getByLabelText(/簇选择距离容差/)).toHaveValue(0);
    const singleCluster = screen.getByRole("checkbox", { name: /允许所有向量形成单一簇/ });
    expect(singleCluster).not.toBeChecked();
    const postprocessing = screen.getByRole("checkbox", { name: /启用声纹合并后处理/ });
    expect(postprocessing).toBeChecked();
    expect(screen.getByLabelText(/触发合并高阈值/)).toHaveValue(0.7);
    expect(screen.getByLabelText(/组内约束低阈值/)).toHaveValue(0.5);

    fireEvent.change(screen.getByLabelText(/最小簇大小/), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText(/簇选择距离容差/), { target: { value: "0.2" } });
    fireEvent.change(screen.getByLabelText(/触发合并高阈值/), { target: { value: "0.75" } });
    fireEvent.change(screen.getByLabelText(/组内约束低阈值/), { target: { value: "0.55" } });
    fireEvent.click(singleCluster);

    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => {
      const saved = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
      expect(saved.nodes.find((node) => node.kind === "speaker_clustering")?.config?.parameters).toEqual({
        min_cluster_size: 3,
        min_samples: 2,
        cluster_selection_epsilon: 0.2,
        allow_single_cluster: true,
      });
      expect(saved.nodes.find((node) => node.kind === "speaker_clustering")?.config?.postprocessing).toEqual({
        processor: "audio_harness.speaker_clustering.complete_link_merge",
        version: 1,
        high_similarity_threshold: 0.75,
        low_similarity_threshold: 0.55,
      });
    });

    fireEvent.click(postprocessing);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => {
      const saved = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
      expect(saved.nodes.find((node) => node.kind === "speaker_clustering")?.config?.postprocessing).toBeNull();
    });
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
    expect(screen.getByRole("region", { name: "语音智能体构建画布" })).toBeVisible();
    expect(await screen.findByLabelText("节点 语音工作流")).toBeVisible();
    for (const label of ["Realtime 输入", "语音工作流", "Responses", "Agent Backend", "TTS", "Realtime 输出"]) {
      expect(screen.getByLabelText(`节点 ${label}`)).toBeVisible();
    }
    expect(screen.getByText("主语音图 · v1")).toBeVisible();
    expect(screen.queryByLabelText("节点 physical-vad")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("节点 asr")).not.toBeInTheDocument();
    expect(api.getActiveAudioWorkflow).toHaveBeenCalledWith("wf-1", expect.any(AbortSignal));
    expect(screen.getByTestId("rf__edge-input-workflow")).toBeInTheDocument();
    expect(screen.getByTestId("rf__edge-workflow-responses")).toBeInTheDocument();
    expect(screen.queryByTestId("rf__edge-workflow-voiceprint")).not.toBeInTheDocument();
    expect(screen.queryByTestId("rf__edge-voiceprint-responses")).not.toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "语音智能体页面" })).getByRole("link", { name: /构建/ })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("link", { name: "概览" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "发布" })).not.toBeInTheDocument();
    const saveButton = screen.getByRole("button", { name: "保存草稿" });
    fireEvent.click(screen.getByRole("button", { name: "自动排列" }));
    await waitFor(() => expect(saveButton).toBeDisabled());

    fireEvent.click(screen.getByLabelText("节点 语音工作流"));
    expect(screen.getByRole("link", { name: "打开语音工作流" })).toHaveAttribute("href", "/audio-workflows/wf-1/build");

    fireEvent.click(screen.getByLabelText("节点 Responses"));
    expect(screen.getByLabelText("智能体节点配置面板")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Responses 模型"), { target: { value: "new-model" } });
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);
    await waitFor(() => expect(saveVoiceAgentDraft).toHaveBeenCalled());
    await waitFor(() => expect(saveButton).toBeDisabled());

    fireEvent.click(within(screen.getByRole("navigation", { name: "语音智能体页面" })).getByRole("link", { name: /接入/ }));
    expect(await screen.findByRole("heading", { name: "发布当前草稿" })).toBeVisible();
    expect(screen.getByRole("button", { name: "编译并发布" })).toBeVisible();
    expect(screen.getByLabelText("语音智能体侧栏")).toBeVisible();
    expect(screen.getByText("/v1/realtime?model=voice-helper")).toBeVisible();
    expect(screen.getByText("/v1/realtime/calls?model=voice-helper")).toBeVisible();

    fireEvent.click(screen.getByRole("link", { name: /测试/ }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "编译并发布" })).not.toBeInTheDocument());
    expect(screen.queryByLabelText("测试 model")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "WSS" })).toHaveClass("active");
    expect(screen.getByRole("button", { name: "WSS" })).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "启用 AHA 扩展协议" })).not.toBeChecked();
  });

  it("redirects the legacy voice agent publish route to access", async () => {
    const { api } = fakeApi(); renderApp(api, "/voice-agents/agent-1/publish");
    expect(await screen.findByRole("heading", { name: "发布当前草稿" })).toBeVisible();
    expect(within(screen.getByRole("navigation", { name: "语音智能体页面" })).getByRole("link", { name: /接入/ })).toHaveAttribute("aria-current", "page");
  });

  it("loads Responses model choices from the selected engine", async () => {
    const { api } = fakeApi(); renderApp(api, "/voice-agents/agent-1/build");
    fireEvent.click(await screen.findByLabelText("节点 Responses"));
    expect(await screen.findByText("已从 LLM-1 获取 2 个模型。")).toBeVisible();
    expect(api.listEngineModels).toHaveBeenCalledWith("llm-1", expect.any(AbortSignal));
    const modelInput = screen.getByRole("combobox", { name: "Responses 模型" });
    fireEvent.click(modelInput);
    expect(screen.getByRole("listbox", { name: "可用 Responses 模型" })).toBeVisible();
    expect(screen.getByRole("option", { name: "deepseek-reasoner" })).toBeVisible();
  });

  it("creates an incomplete voice agent draft in one step", async () => {
    const { api, createVoiceAgent } = fakeApi(); renderApp(api, "/voice-agents/new");
    expect(await screen.findByRole("heading", { name: "新建语音智能体" })).toBeVisible();
    expect(screen.queryByRole("list", { name: "创建步骤" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "Support Agent" } });
    expect(screen.getByDisplayValue("support-agent")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "创建并进入构建" }));
    await waitFor(() => expect(createVoiceAgent).toHaveBeenCalledWith(expect.objectContaining({
      name: "Support Agent",
      slug: "support-agent",
      definition: expect.objectContaining({ audio_workflow_id: "", responses_engine_id: null, tts_engine_id: null }),
    })));
  });

  it("keeps an automatic graph layout local until it is saved", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi(); renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByRole("button", { name: "自动排列" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "保存草稿" })).toBeEnabled());
    expect(saveAudioWorkflowDraft).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
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

    fireEvent.click(screen.getByRole("button", { name: /^物理 \+ 语义 VAD \+ ASR/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));
    expect(saveButton).toBeEnabled();
    await new Promise((resolve) => window.setTimeout(resolve, 800));
    expect(saveAudioWorkflowDraft).not.toHaveBeenCalled();
    fireEvent.click(saveButton);

    await waitFor(() => expect(saveAudioWorkflowDraft).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(saveButton).toBeDisabled());
  });

  it("guards navigation while a workflow has unsaved changes", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi(); renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByRole("button", { name: /^物理 \+ 语义 VAD \+ ASR/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));

    const access = within(screen.getByRole("navigation", { name: "工作流页面" })).getByRole("link", { name: /接入/ });
    fireEvent.click(access);
    expect(await screen.findByRole("alertdialog", { name: "还有未保存的修改" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.getByRole("region", { name: "语音工作流画布" })).toBeVisible();

    fireEvent.click(access);
    fireEvent.click(await screen.findByRole("button", { name: "保存并离开" }));
    await waitFor(() => expect(saveAudioWorkflowDraft).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("heading", { name: "发布当前草稿" })).toBeVisible();
  });

  it("applies a complete semantic VAD template and preserves engine choices", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi(); renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByRole("button", { name: /^物理 \+ 语义 VAD \+ ASR/ }));
    expect(screen.getByRole("alertdialog", { name: "应用快速模板？" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));

    expect(saveAudioWorkflowDraft).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAudioWorkflowDraft).toHaveBeenCalled(), { timeout: 2_000 });
    const savedDocument = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
    expect(savedDocument.nodes.map((node) => node.kind)).toEqual(["physical_vad", "semantic_vad", "asr"]);
    expect(savedDocument.nodes.find((node) => node.kind === "physical_vad")?.engine_id).toBe("vad-1");
    expect(savedDocument.nodes.find((node) => node.kind === "asr")?.engine_id).toBe("asr-1");
    expect(savedDocument.outputs.map((output) => output.name)).toContain("turn_progress");
  });

  it("applies the complete meeting template with a vector search branch", async () => {
    const { api, saveAudioWorkflowDraft } = fakeApi(); renderApp(api, "/audio-workflows/wf-1/build");
    fireEvent.click(await screen.findByRole("button", { name: /^会议系统完全体/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));

    expect(saveAudioWorkflowDraft).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAudioWorkflowDraft).toHaveBeenCalled(), { timeout: 2_000 });
    const savedDocument = saveAudioWorkflowDraft.mock.calls.at(-1)?.[1] as AudioWorkflowDocument;
    expect(savedDocument.nodes.map((node) => node.kind)).toEqual([
      "physical_vad", "semantic_vad", "asr", "physical_vad", "asr", "audio_embedding", "vector_search", "speaker_diarization", "asr", "speaker_clustering",
    ]);
    expect(savedDocument.nodes.find((node) => node.kind === "vector_search")?.config).toMatchObject({
      collection_name: "voiceprints",
      vector_name: "eres2netv2",
    });
    expect(savedDocument.outputs.map((output) => output.name)).toContain("vector_search");
    expect(savedDocument.outputs.map((output) => output.name)).toContain("diarization_local");
    expect(savedDocument.outputs.map((output) => output.name)).toContain("diarization_cluster");
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

  it("fills a new engine slug from its name and preserves a manual override", async () => {
    const { api } = fakeApi(); renderApp(api, "/engines");
    fireEvent.click(await screen.findByRole("button", { name: "注册引擎" }));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "Primary ASR" } });
    expect(screen.getByDisplayValue("primary-asr")).toBeVisible();
    fireEvent.change(screen.getByDisplayValue("primary-asr"), { target: { value: "custom-asr" } });
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "Renamed ASR" } });
    expect(screen.getByDisplayValue("custom-asr")).toBeVisible();
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
