import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type {
  AudioHarnessApi,
  CapacitySettings,
  EngineAsset,
  EngineFilters,
  NodeTypeDefinition,
  PipelineDefinition,
  PipelineDraft,
  WorkflowSettings,
} from "./api/types";

function asset(
  id: string,
  name: string,
  api: EngineAsset["api"],
): EngineAsset {
  return {
    id,
    slug: id,
    name,
    kind: api === "ux_vad" ? "vad" : "asr",
    api,
    protocol: "grpc",
    target:
      api === "ux_vad" ? "127.0.0.1:50053" : "127.0.0.1:50052",
    connect_timeout_seconds: 10,
    request_timeout_seconds: null,
    enabled: true,
    status: "confirmed_running",
    image: null,
    description: "测试引擎",
    status_basis: null,
    status_observed_at: null,
    source: "test",
    details: {},
    revision: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

const nodeTypes: NodeTypeDefinition[] = [
  {
    type: "audio_input",
    display_name: "音频输入",
    input_ports: {},
    output_ports: {
      audio: { payload_type: "audio_chunk", required: true, aggregate: false },
    },
    config_schema: { type: "object", properties: {} },
    allowed_failure_policies: ["fail_pipeline"],
    default_failure_policy: "fail_pipeline",
    required_api: null,
    max_instances: 1,
    source: true,
    sink: false,
    version: 1,
  },
  {
    type: "physical_vad",
    display_name: "物理 VAD",
    input_ports: {
      audio: { payload_type: "audio_chunk", required: true, aggregate: false },
    },
    output_ports: {
      segments: {
        payload_type: "audio_segment",
        required: true,
        aggregate: false,
      },
      events: {
        payload_type: "session_event",
        required: false,
        aggregate: false,
      },
    },
    config_schema: {
      type: "object",
      properties: {
        min_segment_seconds: {
          type: "number",
          default: 0.5,
          minimum: 0,
        },
      },
    },
    allowed_failure_policies: ["fail_pipeline"],
    default_failure_policy: "fail_pipeline",
    required_api: "ux_vad",
    max_instances: 1,
    source: false,
    sink: false,
    version: 1,
  },
  {
    type: "semantic_vad",
    display_name: "语义 VAD",
    input_ports: {
      segments: {
        payload_type: "audio_segment",
        required: true,
        aggregate: false,
      },
    },
    output_ports: {
      segments: {
        payload_type: "audio_segment",
        required: true,
        aggregate: false,
      },
      events: {
        payload_type: "session_event",
        required: false,
        aggregate: false,
      },
    },
    config_schema: { type: "object", properties: {} },
    allowed_failure_policies: ["fail_open", "fail_pipeline"],
    default_failure_policy: "fail_open",
    required_api: "ux_vad",
    max_instances: 1,
    source: false,
    sink: false,
    version: 1,
  },
  {
    type: "asr",
    display_name: "语音识别",
    input_ports: {
      segments: {
        payload_type: "audio_segment",
        required: true,
        aggregate: false,
      },
    },
    output_ports: {
      events: {
        payload_type: "session_event",
        required: true,
        aggregate: false,
      },
    },
    config_schema: { type: "object", properties: {} },
    allowed_failure_policies: ["fail_pipeline"],
    default_failure_policy: "fail_pipeline",
    required_api: "ux_speech",
    max_instances: 1,
    source: false,
    sink: false,
    version: 1,
  },
  {
    type: "session_output",
    display_name: "会话输出",
    input_ports: {
      events: {
        payload_type: "session_event",
        required: true,
        aggregate: true,
      },
    },
    output_ports: {},
    config_schema: { type: "object", properties: {} },
    allowed_failure_policies: ["fail_pipeline"],
    default_failure_policy: "fail_pipeline",
    required_api: null,
    max_instances: 1,
    source: false,
    sink: true,
    version: 1,
  },
];

const pipeline: PipelineDefinition = {
  schema_version: 1,
  nodes: [
    {
      id: "input",
      type: "audio_input",
      display_name: null,
      engine_id: null,
      config: {},
      failure_policy: "fail_pipeline",
      position: { x: 0, y: 120 },
    },
    {
      id: "physical",
      type: "physical_vad",
      display_name: null,
      engine_id: null,
      config: { min_segment_seconds: 0.5 },
      failure_policy: "fail_pipeline",
      position: { x: 240, y: 120 },
    },
    {
      id: "semantic",
      type: "semantic_vad",
      display_name: null,
      engine_id: "vad-engine",
      config: {},
      failure_policy: "fail_open",
      position: { x: 480, y: 120 },
    },
    {
      id: "asr",
      type: "asr",
      display_name: null,
      engine_id: "asr-engine",
      config: {},
      failure_policy: "fail_pipeline",
      position: { x: 720, y: 120 },
    },
    {
      id: "output",
      type: "session_output",
      display_name: null,
      engine_id: null,
      config: {},
      failure_policy: "fail_pipeline",
      position: { x: 960, y: 120 },
    },
  ],
  edges: [
    {
      id: "input_physical",
      source_node: "input",
      source_port: "audio",
      target_node: "physical",
      target_port: "audio",
    },
    {
      id: "physical_semantic",
      source_node: "physical",
      source_port: "segments",
      target_node: "semantic",
      target_port: "segments",
    },
    {
      id: "semantic_asr",
      source_node: "semantic",
      source_port: "segments",
      target_node: "asr",
      target_port: "segments",
    },
    {
      id: "physical_output",
      source_node: "physical",
      source_port: "events",
      target_node: "output",
      target_port: "events",
    },
    {
      id: "semantic_output",
      source_node: "semantic",
      source_port: "events",
      target_node: "output",
      target_port: "events",
    },
    {
      id: "asr_output",
      source_node: "asr",
      source_port: "events",
      target_node: "output",
      target_port: "events",
    },
  ],
};

function fakeApi() {
  const assets = [
    asset("asr-engine", "Qwen ASR", "ux_speech"),
    asset("vad-engine", "FireRed VAD", "ux_vad"),
  ];
  const listEngines = vi.fn(
    async (filters: EngineFilters = {}) => {
      let items = filters.api
        ? assets.filter((item) => item.api === filters.api)
        : assets;
      if (filters.kind) {
        items = items.filter((item) => item.kind === filters.kind);
      }
      if (filters.enabled !== undefined) {
        items = items.filter((item) => item.enabled === filters.enabled);
      }
      if (filters.q) {
        const query = filters.q.toLowerCase();
        items = items.filter(
          (item) =>
            item.name.toLowerCase().includes(query) ||
            item.slug.includes(query) ||
            item.target.includes(query),
        );
      }
      return {
        items,
        total: items.length,
        limit: filters.limit ?? 50,
        offset: filters.offset ?? 0,
      };
    },
  );
  let currentDraft: PipelineDraft = {
    definition: pipeline,
    revision: 1,
    updated_at: "2026-01-01T00:00:00Z",
    active_revision: 1,
  };
  const workflow: WorkflowSettings = {
    min_chunk_bytes: 640,
    default_language_code: "zh-CN",
    audio_queue_capacity: 32,
    segment_queue_capacity: 8,
    result_queue_capacity: 32,
    revision: 1,
    updated_at: "2026-01-01T00:00:00Z",
  };
  const capacity: CapacitySettings = {
    grpc: { max_sessions: 64 },
    realtime: { max_sessions: 64 },
    webrtc: { max_sessions: 64 },
    max_asr_concurrency: 8,
    revision: 1,
    updated_at: "2026-01-01T00:00:00Z",
  };
  const api: AudioHarnessApi = {
    listEngines,
    getEngine: vi.fn(async (engineId) => {
      const engine = assets.find((item) => item.id === engineId);
      if (!engine) {
        throw new Error("engine not found");
      }
      return engine;
    }),
    createEngine: vi.fn(),
    updateEngine: vi.fn(),
    deleteEngine: vi.fn(),
    listNodeTypes: vi.fn(async () => nodeTypes),
    getPipeline: vi.fn(async () => currentDraft),
    updatePipelineDraft: vi.fn(async (definition, expectedRevision) => {
      currentDraft = {
        definition,
        revision: expectedRevision + 1,
        updated_at: "2026-01-02T00:00:00Z",
        active_revision: currentDraft.active_revision,
      };
      return currentDraft;
    }),
    validatePipeline: vi.fn(async () => ({
      valid: true,
      diagnostics: [],
      topological_order: ["input", "physical", "semantic", "asr", "output"],
    })),
    activatePipeline: vi.fn(async () => ({
      definition: currentDraft.definition,
      revision: 2,
      activated_at: "2026-01-02T00:00:00Z",
      engine_revisions: {
        physical: 1,
        semantic: 1,
        asr: 1,
      },
    })),
    getActivePipeline: vi.fn(async () => ({
      definition: pipeline,
      revision: 1,
      activated_at: "2026-01-01T00:00:00Z",
      engine_revisions: {
        semantic: 1,
        asr: 1,
      },
    })),
    getReadiness: vi.fn(async () => ({
      status: "not_ready",
      database: "ready",
      data_plane: "not_ready",
      problems: ["物理 VAD 尚未选择引擎"],
      pipeline_revision: 1,
      nodes: [],
    })),
    getWorkflowSettings: vi.fn(async () => workflow),
    updateWorkflowSettings: vi.fn(async (payload) => ({
      ...payload,
      revision: payload.revision + 1,
      updated_at: "2026-01-02T00:00:00Z",
    })),
    getCapacitySettings: vi.fn(async () => capacity),
    updateCapacitySettings: vi.fn(async (payload) => ({
      ...payload,
      revision: payload.revision + 1,
      updated_at: "2026-01-02T00:00:00Z",
    })),
  };
  return { api, assets, listEngines };
}

function renderApp(
  api: AudioHarnessApi,
  initialEntries: string[] = ["/"],
) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <App api={api} />
    </MemoryRouter>,
  );
}

function stage(name: string): HTMLElement {
  const section = screen
    .getByRole("heading", { name })
    .closest<HTMLElement>("section");
  if (!section) {
    throw new Error(`找不到阶段：${name}`);
  }
  return section;
}

describe("Audio Harness DAG workbench", () => {
  it("opens on orchestration and derives the stage order from graph edges", async () => {
    const { api } = fakeApi();
    renderApp(api);

    expect(
      await screen.findByRole("heading", { name: "默认语音处理链" }),
    ).toBeVisible();
    expect(
      (await screen.findAllByRole("heading", { level: 3 })).map(
        (item) => item.textContent,
      ),
    ).toEqual(["物理 VAD", "语义 VAD", "语音识别"]);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("uses real routes for orchestration, library and settings", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/orchestration"]);

    fireEvent.click(screen.getByRole("link", { name: "引擎库" }));
    expect(await screen.findByRole("heading", { name: "引擎库" })).toBeVisible();
    expect(screen.getByRole("table")).toBeVisible();

    fireEvent.click(screen.getByRole("link", { name: "运行设置" }));
    expect(
      await screen.findByRole("heading", { name: "运行设置" }),
    ).toBeVisible();
  });

  it("loads only enabled API-compatible candidates for a DAG node", async () => {
    const { api, listEngines } = fakeApi();
    renderApp(api, ["/orchestration"]);
    await screen.findByRole("heading", { name: "物理 VAD" });

    fireEvent.click(
      within(stage("物理 VAD")).getByRole("button", {
        name: "选择引擎",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "选择物理 VAD引擎",
    });
    expect(await within(dialog).findByText("FireRed VAD")).toBeVisible();
    expect(within(dialog).queryByText("Qwen ASR")).not.toBeInTheDocument();
    expect(listEngines).toHaveBeenCalledWith(
      expect.objectContaining({ api: "ux_vad", enabled: true }),
      expect.any(AbortSignal),
    );
  });

  it("opens the real topology editor", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/orchestration"]);
    await screen.findByRole("heading", { name: "物理 VAD" });

    fireEvent.click(screen.getByRole("button", { name: "编辑拓扑" }));
    expect(
      await screen.findByRole("heading", { name: "编辑处理链拓扑" }),
    ).toBeVisible();
    expect(screen.getByText("连接类型兼容的端口；服务端校验结果是最终依据。"))
      .toBeVisible();

    expect(
      screen.getByRole("button", { name: "保存工作副本" }),
    ).toBeVisible();
  });

  it("saves, validates and activates a changed work copy explicitly", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/orchestration"]);
    await screen.findByRole("heading", { name: "物理 VAD" });

    fireEvent.click(
      within(stage("物理 VAD")).getByRole("button", { name: "选择引擎" }),
    );
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(await within(dialog).findByText("FireRed VAD"));
    fireEvent.click(
      within(dialog).getByRole("button", { name: "应用到节点" }),
    );

    expect(await screen.findByText("当前有未保存的本地修改")).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "保存工作副本" }),
    );
    await waitFor(() => {
      expect(api.updatePipelineDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          nodes: expect.arrayContaining([
            expect.objectContaining({
              id: "physical",
              engine_id: "vad-engine",
            }),
          ]),
        }),
        1,
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "校验处理链" }));
    await waitFor(() => expect(api.validatePipeline).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "激活修订" }));
    await waitFor(() => expect(api.activatePipeline).toHaveBeenCalledWith(2));
  });

  it("keeps candidate failures scoped inside the picker", async () => {
    const { api, listEngines } = fakeApi();
    listEngines.mockImplementationOnce(async () => ({
      items: [],
      total: 0,
      limit: 200,
      offset: 0,
    }));
    listEngines.mockRejectedValue(new TypeError("Failed to fetch"));
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    renderApp(api, ["/orchestration"]);
    await screen.findByRole("heading", { name: "物理 VAD" });

    fireEvent.click(
      within(stage("物理 VAD")).getByRole("button", { name: "选择引擎" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      await within(dialog).findByText("候选引擎加载失败"),
    ).toBeVisible();
    expect(within(dialog).queryByText("Failed to fetch")).not.toBeInTheDocument();
    consoleError.mockRestore();
  });

  it("keeps node removal in the more menu and confirms work-copy impact", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/orchestration"]);
    await screen.findByText("Qwen ASR");

    const asr = stage("语音识别");
    fireEvent.click(within(asr).getByRole("button", { name: /更多/ }));
    fireEvent.click(
      within(asr).getByRole("menuitem", { name: "移除节点" }),
    );
    expect(
      screen.getByRole("alertdialog", { name: "移除“语音识别”节点" }),
    ).toHaveTextContent("保存和激活前不会影响当前运行修订");
  });

  it("preserves create and delete flows in the independent library", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/engines"]);
    const table = await screen.findByRole("table");

    fireEvent.click(screen.getByRole("button", { name: "新建引擎" }));
    expect(screen.getByRole("heading", { name: "新建引擎" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    const qwen = await within(table).findByText("Qwen ASR");
    const row = qwen.closest<HTMLElement>('[role="row"]')!;
    fireEvent.click(within(row).getByRole("button", { name: /更多/ }));
    fireEvent.click(within(row).getByRole("menuitem", { name: "删除" }));
    expect(
      screen.getByRole("alertdialog", { name: "删除“Qwen ASR”" }),
    ).toBeVisible();
  });

  it("debounces server-side search in the engine library", async () => {
    const { api, listEngines } = fakeApi();
    renderApp(api, ["/engines"]);
    await screen.findByRole("table");

    fireEvent.change(
      screen.getByPlaceholderText("搜索名称、标识或 Endpoint"),
      { target: { value: "qwen" } },
    );
    await waitFor(() => {
      expect(listEngines).toHaveBeenCalledWith(
        expect.objectContaining({ q: "qwen" }),
        expect.any(AbortSignal),
      );
    });
  });

  it("does not expose browser network errors in the library", async () => {
    const { api } = fakeApi();
    api.listEngines = vi.fn(() =>
      Promise.reject(new TypeError("Failed to fetch")),
    );
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    renderApp(api, ["/engines"]);

    expect(
      await screen.findByText("暂时无法连接管理服务，请稍后重试。"),
    ).toBeVisible();
    expect(screen.queryByText("Failed to fetch")).not.toBeInTheDocument();
    consoleError.mockRestore();
  });

  it("preserves global workflow and capacity saving", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/settings"]);
    await screen.findByRole("heading", { name: "运行设置" });

    fireEvent.change(screen.getByLabelText("输入重分块"), {
      target: { value: "1280" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存处理策略" }));
    await waitFor(() => {
      expect(api.updateWorkflowSettings).toHaveBeenCalledWith(
        expect.objectContaining({ min_chunk_bytes: 1280, revision: 1 }),
      );
    });

    fireEvent.change(screen.getByLabelText("最大 ASR 并发"), {
      target: { value: "4" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存容量策略" }));
    await waitFor(() => {
      expect(api.updateCapacitySettings).toHaveBeenCalledWith(
        expect.objectContaining({ max_asr_concurrency: 4, revision: 1 }),
      );
    });
  });

  it("shows a safe page for unknown frontend routes", () => {
    const { api } = fakeApi();
    renderApp(api, ["/missing"]);
    expect(
      screen.getByRole("link", { name: "返回编排工作台" }),
    ).toBeVisible();
  });
});
