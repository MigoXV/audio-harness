import { afterEach, describe, expect, it, vi } from "vitest";

import { apiClient, errorMessage } from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("audio harness API client", () => {
  it("converts browser network errors into user-facing Chinese", () => {
    expect(errorMessage(new TypeError("Failed to fetch"))).toBe(
      "暂时无法连接管理服务，请稍后重试。",
    );
    expect(errorMessage(new Error("保存失败"))).toBe("保存失败");
  });

  it("reads configuration gaps from the always-available runtime overview", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            status: "needs_configuration",
            control_plane: "ready",
            issues: [{ code: "realtime_route_unconfigured", scope: "realtime", severity: "warning", message: "尚未配置公开 Realtime 模型路由", resource_id: null }],
            realtime: { state: "needs_configuration", public_routes: 0, internal_routes: 0 },
          },
        ),
      ),
    );

    await expect(apiClient.getRuntimeOverview()).resolves.toMatchObject({
      status: "needs_configuration",
      issues: [{ code: "realtime_route_unconfigured" }],
    });
  });

  it("preserves the backend error message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: "catalog_conflict",
              message: "已绑定引擎不能删除",
              details: null,
            },
          },
          409,
        ),
      ),
    );

    await expect(apiClient.deleteEngine("engine-1")).rejects.toEqual(
      expect.objectContaining({
        status: 409,
        code: "catalog_conflict",
        message: "已绑定引擎不能删除",
      }),
    );
  });

  it("submits a manual validation request", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ results: [] }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiClient.validateEngines?.()).resolves.toEqual({
      results: [],
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/engines/validate",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("manages and probes Agent Backend resources", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ items: [{ id: "backend-1" }], total: 1, limit: 200, offset: 0 }))
      .mockResolvedValueOnce(jsonResponse({ backend_id: "backend-1", outcome: "healthy", name: "DSH", revision: 1 }))
      .mockResolvedValueOnce(jsonResponse({ backend_id: "backend-1", backend_revision: 1, wire_version: "0.0.1", provider: "deepseek", model: "deepseek-v4-flash", cwd: "/workspace", presets: ["standard"], plugins: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiClient.listAgentBackends?.()).resolves.toEqual([{ id: "backend-1" }]);
    await expect(apiClient.validateAgentBackend?.("backend-1")).resolves.toMatchObject({ outcome: "healthy" });
    await expect(apiClient.getAgentBackendCapabilities?.("backend-1")).resolves.toMatchObject({ presets: ["standard"] });
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/agent-backends/backend-1/validate", expect.objectContaining({ method: "POST" }));
  });

  it("updates workflow settings with the complete revision snapshot", async () => {
    const response = {
      min_chunk_bytes: 640,
      default_language_code: "zh-CN",
      audio_queue_capacity: 32,
      received_audio_cache_enabled: false,
      received_audio_cache_block_seconds: 5,
      received_audio_cache_file_seconds: 300,
      segment_queue_capacity: 8,
      result_queue_capacity: 32,
      revision: 2,
      updated_at: "2026-01-02T00:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(response));
    vi.stubGlobal("fetch", fetchMock);

    const { updated_at, ...payload } = response;
    expect(updated_at).toBe("2026-01-02T00:00:00Z");
    await expect(apiClient.updateWorkflowSettings(payload)).resolves.toEqual(
      response,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/settings/workflow",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify(payload),
      }),
    );
  });

  it("updates realtime capacity with a shared ASR limit", async () => {
    const response = {
      realtime: { max_sessions: 32 },
      webrtc: { max_sessions: 16 },
      max_asr_concurrency: 8,
      revision: 2,
      updated_at: "2026-01-02T00:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(response));
    vi.stubGlobal("fetch", fetchMock);

    const { updated_at, ...payload } = response;
    expect(updated_at).toBe("2026-01-02T00:00:00Z");
    await expect(apiClient.updateCapacitySettings(payload)).resolves.toEqual(
      response,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/settings/capacity",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify(payload),
      }),
    );
  });
});
