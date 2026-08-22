import { describe, expect, it } from "vitest";

import { parseHeadersEnv, serializeHeadersEnv } from "./AgentBackendPage";

describe("Agent Backend 请求头环境变量", () => {
  it("round-trips header to environment mappings without secret values", () => {
    const mapping = { Authorization: "DSH_AUTH_HEADER", "X-API-Key": "DSH_API_KEY" };
    expect(parseHeadersEnv(serializeHeadersEnv(mapping))).toEqual(mapping);
  });

  it("rejects malformed mapping lines", () => {
    expect(() => parseHeadersEnv("Authorization"))
      .toThrow("Header=ENV_NAME");
  });
});
