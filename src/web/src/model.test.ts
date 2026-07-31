import { describe, expect, it } from "vitest";

import type { EngineAsset } from "./api/types";
import {
  apiForKind,
  bindingCandidates,
  capabilityLabel,
  engineState,
  kindLabel,
  PIPELINE_STAGES,
  requiredApi,
  roleLabel,
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
    expect(apiForKind("asr_vad")).toBeNull();
    expect(requiredApi("tts")).toBeNull();
  });

  it("falls back safely for unknown capabilities and roles", () => {
    expect(kindLabel("speaker_diarization")).toBe("说话人分离");
    expect(capabilityLabel("future_capability")).toBe("future_capability");
    expect(roleLabel("tts")).toBe("tts");
  });

  it("describes the fixed runtime execution order without a fake order field", () => {
    expect(PIPELINE_STAGES.map((stage) => stage.role)).toEqual([
      "vad",
      "semantic_vad",
      "asr",
    ]);
    expect(
      PIPELINE_STAGES.filter((stage) => stage.required).map(
        (stage) => stage.role,
      ),
    ).toEqual(["vad", "asr"]);
  });

  it("separates catalog status from live readiness", () => {
    expect(engineState(engine("confirmed", "ux_speech"))).toEqual({
      label: "已确认",
      tone: "confirmed",
    });
    expect(
      engineState({
        ...engine("disabled", "ux_speech"),
        enabled: false,
      }),
    ).toEqual({
      label: "已停用",
      tone: "muted",
    });
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
