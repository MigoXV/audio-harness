import type { AudioNode, AudioWorkflowDocument } from "../api/types";

export type AudioGraphTemplateId = "physical_asr" | "physical_semantic_asr";

type NodeKind = AudioNode["kind"];

function existingNode<T extends NodeKind>(
  document: AudioWorkflowDocument,
  kind: T,
  preferredId: string,
): Extract<AudioNode, { kind: T }> | undefined {
  return document.nodes.find((node) => node.kind === kind && node.id === preferredId) as Extract<AudioNode, { kind: T }> | undefined
    ?? document.nodes.find((node) => node.kind === kind) as Extract<AudioNode, { kind: T }> | undefined;
}

function physicalVad(document: AudioWorkflowDocument) {
  const existing = existingNode(document, "physical_vad", "physical-vad");
  return existing
    ? { ...existing, id: "physical-vad" }
    : {
      id: "physical-vad",
      kind: "physical_vad" as const,
      engine_id: null,
      config: {
        min_segment_seconds: 0.5,
        max_segment_seconds: 60,
        audio_buffer_seconds: 120,
        speech_pre_roll_seconds: 0.3,
        postprocessing: null,
      },
    };
}

function semanticVad(document: AudioWorkflowDocument) {
  const existing = existingNode(document, "semantic_vad", "semantic-vad");
  return existing
    ? { ...existing, id: "semantic-vad" }
    : {
      id: "semantic-vad",
      kind: "semantic_vad" as const,
      engine_id: null,
      config: { max_buffer_seconds: 60 },
      failure_policy: "fail_open" as const,
    };
}

function asr(document: AudioWorkflowDocument) {
  const existing = existingNode(document, "asr", "asr");
  return existing
    ? { ...existing, id: "asr" }
    : { id: "asr", kind: "asr" as const, engine_id: null, config: {} };
}

export function applyAudioGraphTemplate(
  document: AudioWorkflowDocument,
  template: AudioGraphTemplateId,
): AudioWorkflowDocument {
  const physical = physicalVad(document);
  const recognition = asr(document);
  if (template === "physical_asr") {
    return {
      schema_version: 1,
      nodes: [physical, recognition],
      edges: [
        { id: "audio-to-physical-vad", source: { kind: "input", name: "audio" }, target: { node_id: "physical-vad", port: "audio" } },
        { id: "physical-vad-to-asr", source: { kind: "node", node_id: "physical-vad", port: "segments" }, target: { node_id: "asr", port: "segments" } },
      ],
      outputs: [
        { name: "primary_transcript", source: { kind: "node", node_id: "asr", port: "transcripts" } },
        { name: "speech_started", source: { kind: "node", node_id: "physical-vad", port: "speech_started" } },
        { name: "speech_stopped", source: { kind: "node", node_id: "physical-vad", port: "speech_stopped" } },
      ],
      layout: {
        "physical-vad": { x: 240, y: 120 },
        asr: { x: 520, y: 120 },
      },
    };
  }

  const semantic = semanticVad(document);
  return {
    schema_version: 1,
    nodes: [physical, semantic, recognition],
    edges: [
      { id: "audio-to-physical-vad", source: { kind: "input", name: "audio" }, target: { node_id: "physical-vad", port: "audio" } },
      { id: "physical-vad-to-semantic-vad", source: { kind: "node", node_id: "physical-vad", port: "segments" }, target: { node_id: "semantic-vad", port: "segments" } },
      { id: "semantic-vad-to-asr", source: { kind: "node", node_id: "semantic-vad", port: "segments" }, target: { node_id: "asr", port: "segments" } },
    ],
    outputs: [
      { name: "primary_transcript", source: { kind: "node", node_id: "asr", port: "transcripts" } },
      { name: "speech_started", source: { kind: "node", node_id: "physical-vad", port: "speech_started" } },
      { name: "speech_stopped", source: { kind: "node", node_id: "semantic-vad", port: "speech_stopped" } },
      { name: "turn_progress", source: { kind: "node", node_id: "semantic-vad", port: "turn_progress" } },
    ],
    layout: {
      "physical-vad": { x: 240, y: 120 },
      "semantic-vad": { x: 520, y: 120 },
      asr: { x: 800, y: 120 },
    },
  };
}
