import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type {
  AudioHarnessApi,
  EngineAsset,
  EngineBinding,
  EngineFilters,
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
    target: api === "ux_vad" ? "127.0.0.1:50053" : "127.0.0.1:50052",
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
      const items = filters.api
        ? assets.filter((item) => item.api === filters.api)
        : assets;
      return {
        items,
        total: items.length,
        limit: filters.limit ?? 50,
        offset: filters.offset ?? 0,
      };
    },
  );
  const emptyBindings: EngineBinding[] = [
    { role: "asr", engine: null, generation: 0, updated_at: null },
    { role: "vad", engine: null, generation: 0, updated_at: null },
    {
      role: "semantic_vad",
      engine: null,
      generation: 0,
      updated_at: null,
    },
  ];
  const api: AudioHarnessApi = {
    listEngines,
    createEngine: vi.fn(),
    updateEngine: vi.fn(),
    deleteEngine: vi.fn(),
    listBindings: vi.fn(async () => emptyBindings),
    bind: vi.fn(),
    unbind: vi.fn(),
    getReadiness: vi.fn(async () => ({
      status: "not_ready",
      database: "ready",
      data_plane: "not_ready",
      problems: ["ASR 未绑定", "VAD 未绑定"],
    })),
  };
  return { api, listEngines };
}

describe("Audio Harness workbench", () => {
  it("loads engine assets and exposes compatible binding choices", async () => {
    const { api } = fakeApi();
    render(<App api={api} />);

    expect((await screen.findAllByText("Qwen ASR")).length).toBeGreaterThan(0);
    const asrSelect = screen.getByLabelText("语音识别候选引擎");
    const vadSelect = screen.getByLabelText("物理 VAD候选引擎");

    expect(asrSelect).toHaveTextContent("Qwen ASR");
    expect(asrSelect).not.toHaveTextContent("FireRed VAD");
    expect(vadSelect).toHaveTextContent("FireRed VAD");
    expect(vadSelect).not.toHaveTextContent("Qwen ASR");
  });

  it("keeps kind and interface consistent in the create form", async () => {
    const { api } = fakeApi();
    render(<App api={api} />);
    await screen.findAllByText("Qwen ASR");

    fireEvent.click(screen.getByRole("button", { name: "新建引擎" }));
    fireEvent.change(screen.getByLabelText("引擎类型"), {
      target: { value: "vad" },
    });

    expect(screen.getByLabelText("接口")).toHaveValue("ux_vad");
  });

  it("debounces server-side search filters", async () => {
    const { api, listEngines } = fakeApi();
    render(<App api={api} />);
    await screen.findAllByText("Qwen ASR");

    fireEvent.change(screen.getByPlaceholderText("搜索名称、slug 或 gRPC 地址"), {
      target: { value: "qwen" },
    });

    await waitFor(() => {
      expect(listEngines).toHaveBeenCalledWith(
        expect.objectContaining({ q: "qwen" }),
        expect.any(AbortSignal),
      );
    });
  });
});
