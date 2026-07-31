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
    expect(errorMessage(new Error("internal stack detail"))).toBe(
      "操作未完成，请稍后重试。",
    );
  });

  it("treats readiness 503 as a valid not-ready state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            status: "not_ready",
            database: "ready",
            data_plane: "not_ready",
            problems: ["ASR 未绑定"],
            pipeline_revision: null,
            nodes: [],
          },
          503,
        ),
      ),
    );

    await expect(apiClient.getReadiness()).resolves.toMatchObject({
      status: "not_ready",
      problems: ["ASR 未绑定"],
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
      "/api/v1/workflow-settings",
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
      "/api/v1/capacity-settings",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify(payload),
      }),
    );
  });
});
