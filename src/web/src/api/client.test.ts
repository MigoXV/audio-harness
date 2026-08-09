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
            ready: false,
            control_plane: "ready",
            problems: ["尚未绑定 gRPC 语音工作流"],
            issues: [{ code: "grpc_workflow_unconfigured", scope: "grpc", message: "尚未绑定 gRPC 语音工作流" }],
            grpc: { status: "needs_configuration", workflow_id: null, workflow_revision: null },
            realtime: { status: "needs_configuration", public_routes: 0, internal_routes: 0, invalid_routes: 0 },
          },
        ),
      ),
    );

    await expect(apiClient.getRuntimeOverview()).resolves.toMatchObject({
      status: "needs_configuration",
      problems: ["尚未绑定 gRPC 语音工作流"],
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

  it("updates workflow settings with the complete revision snapshot", async () => {
    const response = {
      min_chunk_bytes: 640,
      default_language_code: "zh-CN",
      audio_queue_capacity: 32,
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

  it("updates per-protocol capacity with a shared ASR limit", async () => {
    const response = {
      grpc: { max_sessions: 64 },
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
