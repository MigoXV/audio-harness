import { afterEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "./client";

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
});
