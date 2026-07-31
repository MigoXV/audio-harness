import { describe, expect, it } from "vitest";

import type {
  EngineAsset,
  PipelineDefinition,
} from "./api/types";
import {
  apiForKind,
  capabilityLabel,
  engineState,
  kindLabel,
  nodeCandidates,
  nodeTypeLabel,
  slugify,
  topologicalNodes,
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

describe("DAG model helpers", () => {
  it("maps known node types and keeps unknown values visible", () => {
    expect(apiForKind("asr_vad")).toBeNull();
    expect(kindLabel("speaker_diarization")).toBe("说话人分离");
    expect(capabilityLabel("future_capability")).toBe("future_capability");
    expect(nodeTypeLabel("custom_node")).toBe("custom_node");
  });

  it("only exposes enabled API-compatible node candidates", () => {
    const engines = [
      engine("asr-on", "ux_speech"),
      engine("asr-off", "ux_speech", false),
      engine("vad-on", "ux_vad"),
    ];
    expect(
      nodeCandidates("ux_speech", engines).map((item) => item.id),
    ).toEqual(["asr-on"]);
    expect(
      nodeCandidates("ux_vad", engines).map((item) => item.id),
    ).toEqual(["vad-on"]);
  });

  it("derives a stable topological order from real edges", () => {
    const definition: PipelineDefinition = {
      schema_version: 1,
      nodes: ["output", "asr", "input"].map((id) => ({
        id,
        type: id === "input" ? "audio_input" : id,
        display_name: null,
        engine_id: null,
        config: {},
        failure_policy: "fail_pipeline",
        position: { x: 0, y: 0 },
      })),
      edges: [
        {
          id: "input_asr",
          source_node: "input",
          source_port: "audio",
          target_node: "asr",
          target_port: "segments",
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
    expect(topologicalNodes(definition).map((node) => node.id)).toEqual([
      "input",
      "asr",
      "output",
    ]);
  });

  it("separates catalog status from live node readiness", () => {
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

  it("creates backend-compatible slugs", () => {
    expect(slugify(" Qwen ASR / Production ")).toBe(
      "qwen-asr-production",
    );
  });
});
