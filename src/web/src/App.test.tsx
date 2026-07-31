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
  EngineBinding,
  EngineFilters,
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
  const bindings: EngineBinding[] = [
    {
      role: "asr",
      engine: assets[0],
      generation: 2,
      updated_at: "2026-01-01T00:00:00Z",
    },
    { role: "vad", engine: null, generation: 0, updated_at: null },
    {
      role: "semantic_vad",
      engine: null,
      generation: 0,
      updated_at: null,
    },
  ];
  const workflow: WorkflowSettings = {
    min_chunk_bytes: 640,
    min_segment_seconds: 0.5,
    max_segment_seconds: 60,
    audio_buffer_seconds: 120,
    speech_pre_roll_seconds: 0.3,
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
    createEngine: vi.fn(),
    updateEngine: vi.fn(),
    deleteEngine: vi.fn(),
    listBindings: vi.fn(async () => bindings),
    bind: vi.fn(async (role, engineId) => ({
      role,
      engine: assets.find((item) => item.id === engineId) ?? null,
      generation: 1,
      updated_at: "2026-01-02T00:00:00Z",
    })),
    unbind: vi.fn(),
    getReadiness: vi.fn(async () => ({
      status: "not_ready",
      database: "ready",
      data_plane: "not_ready",
      problems: ["缺少 vad 绑定"],
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

describe("Audio Harness workbench", () => {
  it("opens on orchestration and shows the real fixed processing order", async () => {
    const { api, listEngines } = fakeApi();
    renderApp(api);

    expect(
      await screen.findByRole("heading", { name: "默认语音处理链" }),
    ).toBeVisible();
    const headings = screen
      .getAllByRole("heading", { level: 3 })
      .map((heading) => heading.textContent);
    expect(headings).toEqual(["物理 VAD", "语义 VAD", "语音识别"]);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(listEngines).not.toHaveBeenCalled();
  });

  it("uses real navigation routes for orchestration, library and settings", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/orchestration"]);

    fireEvent.click(screen.getByRole("link", { name: "引擎库" }));
    expect(
      await screen.findByRole("heading", { name: "引擎库" }),
    ).toBeVisible();
    expect(screen.getByRole("table")).toBeVisible();

    fireEvent.click(screen.getByRole("link", { name: "运行设置" }));
    expect(
      await screen.findByRole("heading", { name: "运行设置" }),
    ).toBeVisible();
  });

  it("loads only enabled API-compatible candidates when a stage opens", async () => {
    const { api, listEngines } = fakeApi();
    renderApp(api, ["/orchestration"]);
    await screen.findByRole("heading", { name: "默认语音处理链" });

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
      expect.objectContaining({
        api: "ux_vad",
        enabled: true,
      }),
      expect.any(AbortSignal),
    );
  });

  it("makes validation and immediate application explicit", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/orchestration"]);
    await screen.findByRole("heading", { name: "默认语音处理链" });

    fireEvent.click(
      within(stage("物理 VAD")).getByRole("button", {
        name: "选择引擎",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "选择物理 VAD引擎",
    });
    const candidate = await within(dialog).findByText("FireRed VAD");
    fireEvent.click(candidate);
    expect(
      within(dialog).getByText(/成功后立即替换当前绑定并用于后续会话/),
    ).toBeVisible();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "确认绑定" }),
    );

    await waitFor(() => {
      expect(api.bind).toHaveBeenCalledWith("vad", "vad-engine");
    });
  });

  it("keeps candidate failures scoped inside the picker", async () => {
    const { api } = fakeApi();
    api.listEngines = vi.fn(() =>
      Promise.reject(new TypeError("Failed to fetch")),
    );
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    renderApp(api, ["/orchestration"]);
    await screen.findByRole("heading", { name: "默认语音处理链" });

    fireEvent.click(
      within(stage("物理 VAD")).getByRole("button", {
        name: "选择引擎",
      }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      await within(dialog).findByText("候选引擎加载失败"),
    ).toBeVisible();
    expect(within(dialog).queryByText("Failed to fetch")).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "默认语音处理链" }),
    ).toBeVisible();
    consoleError.mockRestore();
  });

  it("keeps unbind in the more menu and confirms its real impact", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/orchestration"]);
    await screen.findByText("Qwen ASR");

    const asr = stage("语音识别");
    fireEvent.click(within(asr).getByRole("button", { name: /更多/ }));
    fireEvent.click(
      within(asr).getByRole("menuitem", { name: "解除绑定" }),
    );
    expect(
      screen.getByRole("alertdialog", { name: "解除语音识别绑定" }),
    ).toHaveTextContent("数据面会进入未就绪状态");
    expect(api.unbind).not.toHaveBeenCalled();
  });

  it("preserves create and delete flows in the independent library", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/engines"]);
    const table = await screen.findByRole("table");

    fireEvent.click(screen.getByRole("button", { name: "新建引擎" }));
    expect(
      screen.getByRole("heading", { name: "新建引擎" }),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    const qwen = await within(table).findByText("Qwen ASR");
    const row = qwen.closest<HTMLElement>('[role="row"]');
    expect(row).not.toBeNull();
    fireEvent.click(within(row!).getByRole("button", { name: /更多/ }));
    fireEvent.click(within(row!).getByRole("menuitem", { name: "删除" }));
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

  it("preserves workflow and capacity saving on the settings route", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/settings"]);

    expect(
      await screen.findByRole("heading", { name: "运行设置" }),
    ).toBeVisible();
    fireEvent.change(screen.getByLabelText("轮转音频缓冲"), {
      target: { value: "180" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存处理策略" }));

    await waitFor(() => {
      expect(api.updateWorkflowSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          audio_buffer_seconds: 180,
          revision: 1,
        }),
      );
    });

    fireEvent.change(screen.getByLabelText("最大 ASR 并发"), {
      target: { value: "4" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存容量策略" }));
    await waitFor(() => {
      expect(api.updateCapacitySettings).toHaveBeenCalledWith(
        expect.objectContaining({
          max_asr_concurrency: 4,
          revision: 1,
        }),
      );
    });
  });

  it("shows a safe page for unknown frontend routes", async () => {
    const { api } = fakeApi();
    renderApp(api, ["/missing"]);
    expect(
      screen.getByRole("link", { name: "返回编排工作台" }),
    ).toBeVisible();
  });
});
