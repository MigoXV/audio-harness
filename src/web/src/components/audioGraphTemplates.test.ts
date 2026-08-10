import { describe, expect, it } from "vitest";

import type { AudioWorkflowDocument } from "../api/types";
import { applyAudioGraphTemplate } from "./audioGraphTemplates";

const existing: AudioWorkflowDocument = {
  schema_version: 1,
  nodes: [
    { id: "custom-vad", kind: "physical_vad", engine_id: "vad-1", config: { min_segment_seconds: 1, max_segment_seconds: 30, audio_buffer_seconds: 40, speech_pre_roll_seconds: 0.2 } },
    { id: "custom-semantic", kind: "semantic_vad", engine_id: "semantic-1", config: { max_buffer_seconds: 20 }, failure_policy: "fail_pipeline" },
    { id: "custom-asr", kind: "asr", engine_id: "asr-1", config: {} },
  ],
  edges: [],
  outputs: [],
  layout: {},
};

describe("audio graph templates", () => {
  it("builds the physical VAD and ASR graph while preserving matching configuration", () => {
    const result = applyAudioGraphTemplate(existing, "physical_asr");

    expect(result.nodes.map((node) => [node.id, node.kind, node.engine_id])).toEqual([
      ["physical-vad", "physical_vad", "vad-1"],
      ["asr", "asr", "asr-1"],
    ]);
    expect(result.edges).toHaveLength(2);
    expect(result.outputs.map((output) => output.name)).toEqual(["primary_transcript", "speech_started", "speech_stopped"]);
    expect(result.layout?.asr.x).toBeGreaterThan(result.layout?.["physical-vad"].x ?? 0);
  });

  it("builds the semantic VAD graph with event and progress outputs", () => {
    const result = applyAudioGraphTemplate(existing, "physical_semantic_asr");
    const semantic = result.nodes.find((node) => node.kind === "semantic_vad");

    expect(result.nodes.map((node) => node.kind)).toEqual(["physical_vad", "semantic_vad", "asr"]);
    expect(semantic).toMatchObject({ id: "semantic-vad", engine_id: "semantic-1", failure_policy: "fail_pipeline" });
    expect(result.edges.map((edge) => edge.id)).toEqual([
      "audio-to-physical-vad",
      "physical-vad-to-semantic-vad",
      "semantic-vad-to-asr",
    ]);
    expect(result.outputs).toContainEqual({
      name: "speech_stopped",
      source: { kind: "node", node_id: "semantic-vad", port: "speech_stopped" },
    });
    expect(result.outputs).toContainEqual({
      name: "turn_progress",
      source: { kind: "node", node_id: "semantic-vad", port: "turn_progress" },
    });
  });
});
