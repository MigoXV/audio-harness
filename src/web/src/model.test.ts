import { describe, expect, it } from "vitest";

import type { EngineAsset } from "./api/types";
import {
  apiForKind,
  bindingCandidates,
  requiredApi,
  slugify,
} from "./model";

const engine = (
  id: string,
  api: EngineAsset["api"],
  enabled = true,
): EngineAsset => ({
  id,
  slug: id,
  name: id,
  kind: api === "ux_vad" ? "vad" : "asr",
  api,
  protocol: "grpc",
  target: "127.0.0.1:50052",
  connect_timeout_seconds: 10,
  request_timeout_seconds: null,
  enabled,
  status: "confirmed_running",
  image: null,
  description: null,
  status_basis: null,
  status_observed_at: null,
  source: null,
  details: {},
  revision: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
});

describe("engine model helpers", () => {
  it("maps roles and kinds to compatible APIs", () => {
    expect(requiredApi("asr")).toBe("ux_speech");
    expect(requiredApi("vad")).toBe("ux_vad");
    expect(requiredApi("semantic_vad")).toBe("ux_vad");
    expect(apiForKind("asr_vad")).toBe("ux_speech");
  });

  it("only exposes enabled compatible binding candidates", () => {
    const engines = [
      engine("asr-on", "ux_speech"),
      engine("asr-off", "ux_speech", false),
      engine("vad-on", "ux_vad"),
    ];
    expect(bindingCandidates("asr", engines).map((item) => item.id)).toEqual([
      "asr-on",
    ]);
    expect(bindingCandidates("vad", engines).map((item) => item.id)).toEqual([
      "vad-on",
    ]);
  });

  it("creates backend-compatible slugs", () => {
    expect(slugify(" Qwen ASR / Production ")).toBe("qwen-asr-production");
  });
});
