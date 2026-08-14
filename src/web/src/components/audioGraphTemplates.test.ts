import { describe, expect, it } from "vitest";

import type { AudioWorkflowDocument } from "../api/types";
import { applyAudioGraphTemplate } from "./audioGraphTemplates";

const existing: AudioWorkflowDocument = {
  schema_version: 1,
  nodes: [
    { id: "custom-vad", kind: "physical_vad", engine_id: "vad-1", config: { min_segment_seconds: 1, max_segment_seconds: 30, audio_buffer_seconds: 40, speech_pre_roll_seconds: 0.2 } },
    { id: "custom-semantic", kind: "semantic_vad", engine_id: "semantic-1", config: { max_buffer_seconds: 20 }, failure_policy: "fail_pipeline" },
    { id: "custom-asr", kind: "asr", engine_id: "asr-1", config: { remove_punctuation: false } },
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
    expect(result.nodes.find((node) => node.id === "asr")?.config).toMatchObject({ remove_punctuation: false });
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

  it("builds a dual physical VAD graph with an editable interim branch", () => {
    const result = applyAudioGraphTemplate(existing, "dual_physical_asr");
    const previewVad = result.nodes.find((node) => node.id === "interim-physical-vad");

    expect(result.nodes.map((node) => node.id)).toEqual([
      "physical-vad",
      "asr",
      "interim-physical-vad",
      "interim-asr",
    ]);
    expect(result.outputs).toContainEqual({
      name: "interim_transcript",
      source: { kind: "node", node_id: "interim-asr", port: "transcripts" },
    });
    expect(previewVad).toMatchObject({
      kind: "physical_vad",
      engine_id: "vad-1",
      config: {
        max_segment_seconds: 2,
        speech_pre_roll_seconds: 0,
        postprocessing: {
          parameters: {
            speech_threshold: 0.35,
            min_silence_frame: 10,
          },
        },
      },
    });
    expect(result.nodes.find((node) => node.id === "interim-asr")?.engine_id).toBe("asr-1");
    expect(result.nodes.find((node) => node.id === "interim-asr")?.config).toMatchObject({ remove_punctuation: true });
    expect(result.nodes.find((node) => node.id === "asr")?.config).toMatchObject({ remove_punctuation: false });
  });

  it("builds a dual graph with semantic VAD on the primary branch", () => {
    const result = applyAudioGraphTemplate(existing, "dual_physical_semantic_asr");
    const semantic = result.nodes.find((node) => node.id === "semantic-vad");
    const previewVad = result.nodes.find((node) => node.id === "interim-physical-vad");

    expect(result.nodes.map((node) => node.id)).toEqual([
      "physical-vad",
      "semantic-vad",
      "asr",
      "interim-physical-vad",
      "interim-asr",
    ]);
    expect(result.edges.map((edge) => edge.id)).toEqual([
      "audio-to-physical-vad",
      "physical-vad-to-semantic-vad",
      "semantic-vad-to-asr",
      "audio-to-interim-physical-vad",
      "interim-physical-vad-to-asr",
    ]);
    expect(result.outputs).toEqual([
      { name: "primary_transcript", source: { kind: "node", node_id: "asr", port: "transcripts" } },
      { name: "interim_transcript", source: { kind: "node", node_id: "interim-asr", port: "transcripts" } },
      { name: "speech_started", source: { kind: "node", node_id: "physical-vad", port: "speech_started" } },
      { name: "speech_stopped", source: { kind: "node", node_id: "semantic-vad", port: "speech_stopped" } },
      { name: "turn_progress", source: { kind: "node", node_id: "semantic-vad", port: "turn_progress" } },
    ]);
    expect(semantic).toMatchObject({
      kind: "semantic_vad",
      engine_id: "semantic-1",
      config: { max_buffer_seconds: 20 },
      failure_policy: "fail_pipeline",
    });
    expect(previewVad).toMatchObject({
      kind: "physical_vad",
      engine_id: "vad-1",
      config: {
        max_segment_seconds: 2,
        postprocessing: { parameters: { speech_threshold: 0.35, min_silence_frame: 10 } },
      },
    });
    expect(result.nodes.find((node) => node.id === "interim-asr")?.config).toMatchObject({ remove_punctuation: true });
    expect(result.nodes.find((node) => node.id === "asr")?.config).toMatchObject({ remove_punctuation: false });
  });

  it("preserves an explicit punctuation choice on an existing interim ASR", () => {
    const dual = applyAudioGraphTemplate(existing, "dual_physical_asr");
    const preview = dual.nodes.find((node) => node.id === "interim-asr");
    if (!preview || preview.kind !== "asr") throw new Error("interim ASR missing");
    preview.config = { remove_punctuation: false };

    const result = applyAudioGraphTemplate(dual, "dual_physical_asr");

    expect(result.nodes.find((node) => node.id === "interim-asr")?.config).toMatchObject({ remove_punctuation: false });
  });
});
