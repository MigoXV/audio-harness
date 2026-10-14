import { describe, expect, it } from "vitest";

import type { AudioWorkflowDocument, EngineAsset } from "../api/types";
import { applyAudioGraphTemplate } from "./audioGraphTemplates";

const existing: AudioWorkflowDocument = {
  schema_version: 1,
  nodes: [
    { id: "custom-vad", kind: "physical_vad", engine_id: "vad-1", config: { min_segment_seconds: 1, max_segment_seconds: 30, audio_buffer_seconds: 40, speech_pre_roll_seconds: 0.2, speech_chunk_seconds: 1, speech_chunk_holdback_seconds: 1, progress_interval_seconds: 0.2 } },
    { id: "custom-semantic", kind: "semantic_vad", engine_id: "semantic-1", config: { max_buffer_seconds: 20 }, failure_policy: "fail_pipeline" },
    { id: "custom-asr", kind: "asr", engine_id: "asr-1", config: { remove_punctuation: false } },
  ],
  edges: [],
  outputs: [],
  layout: {},
};

describe("audio graph templates", () => {
  it("builds the streaming meeting with two inputs and compatible outputs", () => {
    const result = applyAudioGraphTemplate(existing, "meeting_streaming", [], [
      { id: "duplex", api: "ux_speaker_diarization_duplex", enabled: true } as EngineAsset,
    ]);
    const node = result.nodes.find((value) => value.kind === "streaming_speaker_diarization")!;
    expect(node.engine_id).toBe("duplex");
    expect(result.nodes.some((value) => value.kind === "speaker_clustering" || value.kind === "speaker_diarization")).toBe(false);
    expect(result.edges.filter((edge) => edge.target.node_id === node.id).map((edge) => edge.target.port).sort()).toEqual(["audio", "segments"]);
    expect(result.edges.find((edge) => edge.target.node_id === node.id && edge.target.port === "segments")?.source).toEqual({ kind: "node", node_id: "physical-vad", port: "speech_chunks" });
    expect(result.edges).toContainEqual({ id: "physical-vad-to-semantic-vad", source: { kind: "node", node_id: "physical-vad", port: "segments" }, target: { node_id: "semantic-vad", port: "segments" } });
    expect(result.outputs.find((value) => value.name === "diarization_cluster")?.source).toEqual({ kind: "node", node_id: node.id, port: "cluster_results" });
    expect(result.outputs.find((value) => value.name === "diarization_local")?.source.port).toBe("local_results");
    expect(result.outputs.find((value) => value.name === "speaker_transcript")?.source.node_id).toBe("speaker-asr");
    const modified = { ...result, nodes: result.nodes.map((value) => value.kind === "streaming_speaker_diarization" ? { ...value, config: { ...value.config!, input_buffer_latency_ms: 1040 as const } } : value) };
    const reapplied = applyAudioGraphTemplate(modified, "meeting_streaming");
    expect(reapplied.nodes.find((value) => value.kind === "streaming_speaker_diarization")).toMatchObject({ engine_id: "duplex", config: { input_buffer_latency_ms: 1040 } });
    const nodeIds = new Set(reapplied.nodes.map((value) => value.id));
    expect(reapplied.edges.every((edge) => nodeIds.has(edge.target.node_id) && (edge.source.kind === "input" || nodeIds.has(edge.source.node_id)))).toBe(true);
  });

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

  it("builds a fully connected fail-closed voice agent graph", () => {
    const result = applyAudioGraphTemplate(existing, "voice_agent", [{
      collection_name: "voiceprints",
      vectors: [{ name: "eres2netv2", size: 192, distance: "cosine" }],
      template: "voiceprint",
    }]);

    expect(result.nodes.map((node) => node.id)).toEqual([
      "physical-vad",
      "semantic-vad",
      "audio-embedding",
      "vector-search",
      "speaker-filter",
      "asr",
    ]);
    expect(result.nodes.find((node) => node.kind === "speaker_filter")).toMatchObject({
      id: "speaker-filter",
      config: { allowed_speaker_ids: ["speaker-001"], min_score: 0.75 },
    });
    expect(result.edges).toEqual([
      { id: "audio-to-physical-vad", source: { kind: "input", name: "audio" }, target: { node_id: "physical-vad", port: "audio" } },
      { id: "physical-vad-to-semantic-vad", source: { kind: "node", node_id: "physical-vad", port: "segments" }, target: { node_id: "semantic-vad", port: "segments" } },
      { id: "semantic-vad-to-audio-embedding", source: { kind: "node", node_id: "semantic-vad", port: "segments" }, target: { node_id: "audio-embedding", port: "segments" } },
      { id: "audio-embedding-to-vector-search", source: { kind: "node", node_id: "audio-embedding", port: "embeddings" }, target: { node_id: "vector-search", port: "embeddings" } },
      { id: "semantic-vad-to-speaker-filter", source: { kind: "node", node_id: "semantic-vad", port: "segments" }, target: { node_id: "speaker-filter", port: "segments" } },
      { id: "vector-search-to-speaker-filter", source: { kind: "node", node_id: "vector-search", port: "results" }, target: { node_id: "speaker-filter", port: "results" } },
      { id: "speaker-filter-to-asr", source: { kind: "node", node_id: "speaker-filter", port: "segments" }, target: { node_id: "asr", port: "segments" } },
    ]);
    expect(result.outputs).toEqual([
      { name: "primary_transcript", source: { kind: "node", node_id: "asr", port: "transcripts" } },
      { name: "speech_started", source: { kind: "node", node_id: "speaker-filter", port: "speech_started" } },
      { name: "speech_stopped", source: { kind: "node", node_id: "speaker-filter", port: "speech_stopped" } },
      { name: "turn_progress", source: { kind: "node", node_id: "semantic-vad", port: "turn_progress" } },
      { name: "vector_search", source: { kind: "node", node_id: "vector-search", port: "results" } },
      { name: "speaker_filter", source: { kind: "node", node_id: "speaker-filter", port: "decisions" } },
    ]);
  });

  it("builds the complete meeting graph with vector search on semantic segments", () => {
    const result = applyAudioGraphTemplate(existing, "meeting_complete", [{
      collection_name: "meeting-features",
      vectors: [{ name: "meeting-model", size: 256, distance: "cosine" }],
      template: "voiceprint",
    }]);

    expect(result.nodes.map((node) => node.id)).toEqual([
      "physical-vad",
      "semantic-vad",
      "asr",
      "interim-physical-vad",
      "interim-asr",
      "audio-embedding",
      "vector-search",
      "speaker-diarization",
      "speaker-asr",
      "speaker-clustering",
    ]);
    expect(result.edges).toContainEqual({
      id: "semantic-vad-to-audio-embedding",
      source: { kind: "node", node_id: "semantic-vad", port: "segments" },
      target: { node_id: "audio-embedding", port: "segments" },
    });
    expect(result.edges).toContainEqual({
      id: "audio-embedding-to-vector-search",
      source: { kind: "node", node_id: "audio-embedding", port: "embeddings" },
      target: { node_id: "vector-search", port: "embeddings" },
    });
    expect(result.edges).toContainEqual({
      id: "semantic-vad-to-speaker-diarization",
      source: { kind: "node", node_id: "semantic-vad", port: "segments" },
      target: { node_id: "speaker-diarization", port: "segments" },
    });
    expect(result.edges).toContainEqual({
      id: "semantic-vad-to-asr",
      source: { kind: "node", node_id: "semantic-vad", port: "segments" },
      target: { node_id: "asr", port: "segments" },
    });
    expect(result.edges).toContainEqual({
      id: "speaker-diarization-to-speaker-asr",
      source: { kind: "node", node_id: "speaker-diarization", port: "segments" },
      target: { node_id: "speaker-asr", port: "segments" },
    });
    expect(result.nodes.find((node) => node.id === "audio-embedding")).toMatchObject({
      kind: "audio_embedding",
      config: { algorithm: "meeting-model", expected_dimension: 256, normalize: true },
    });
    expect(result.nodes.find((node) => node.id === "vector-search")).toMatchObject({
      kind: "vector_search",
      config: { collection_name: "meeting-features", vector_name: "meeting-model", top_k: 10 },
    });
    expect(result.outputs).toContainEqual({
      name: "vector_search",
      source: { kind: "node", node_id: "vector-search", port: "results" },
    });
    expect(result.nodes.find((node) => node.id === "speaker-diarization")).toMatchObject({
      kind: "speaker_diarization",
      config: {
        min_batch_seconds: 60,
        max_batch_seconds: 120,
        min_asr_segment_seconds: 0.5,
        postprocessing: {
          processor: "audio_harness.diarization.vad_coverage",
          version: 1,
        },
      },
    });
    expect(result.nodes.find((node) => node.id === "speaker-clustering")).toMatchObject({
      kind: "speaker_clustering",
      config: {
        clustering_algorithm: "hdbscan",
        parameters: {
          min_cluster_size: 2,
          min_samples: 2,
          cluster_selection_epsilon: 0,
          allow_single_cluster: false,
        },
        postprocessing: {
          processor: "audio_harness.speaker_clustering.complete_link_merge",
          version: 1,
          high_similarity_threshold: 0.70,
          low_similarity_threshold: 0.50,
        },
      },
    });
    expect(result.outputs).toContainEqual({
      name: "diarization_local",
      source: { kind: "node", node_id: "speaker-diarization", port: "local_results" },
    });
    expect(result.outputs).toContainEqual({
      name: "diarization_cluster",
      source: { kind: "node", node_id: "speaker-clustering", port: "results" },
    });
    expect(result.outputs).toContainEqual({
      name: "speaker_transcript",
      source: { kind: "node", node_id: "speaker-asr", port: "speaker_transcripts" },
    });
    expect(result.nodes.find((node) => node.id === "speaker-asr")).toMatchObject({
      kind: "asr",
      engine_id: "asr-1",
    });
  });

  it("preserves the speaker ASR configuration when reapplying the complete meeting template", () => {
    const first = applyAudioGraphTemplate(existing, "meeting_complete");
    const speakerRecognition = first.nodes.find((node) => node.id === "speaker-asr");
    if (!speakerRecognition || speakerRecognition.kind !== "asr") {
      throw new Error("speaker ASR missing");
    }
    speakerRecognition.engine_id = "speaker-asr-1";
    speakerRecognition.config = { remove_punctuation: true };

    const result = applyAudioGraphTemplate(first, "meeting_complete");

    expect(result.nodes.find((node) => node.id === "speaker-asr")).toMatchObject({
      engine_id: "speaker-asr-1",
      config: { remove_punctuation: true },
    });
  });

  it("uses any enabled embedding engine for speaker clustering", () => {
    const embeddingEngine = {
      id: "embedding-without-capabilities",
      enabled: true,
      api: "engine_feature",
      details: {},
    } as EngineAsset;

    const result = applyAudioGraphTemplate(
      existing,
      "meeting_complete",
      [],
      [embeddingEngine],
    );

    expect(result.nodes.find((node) => node.kind === "speaker_clustering"))
      .toMatchObject({ engine_id: embeddingEngine.id });
  });

  it("preserves configured vector nodes when reapplying the complete meeting template", () => {
    const first = applyAudioGraphTemplate(existing, "meeting_complete");
    const embedding = first.nodes.find((node) => node.id === "audio-embedding");
    const search = first.nodes.find((node) => node.id === "vector-search");
    if (!embedding || embedding.kind !== "audio_embedding" || !search || search.kind !== "vector_search") {
      throw new Error("vector branch missing");
    }
    embedding.engine_id = "embedding-1";
    embedding.config = { algorithm: "custom-model", expected_dimension: 384, normalize: false };
    search.config = { ...search.config, collection_name: "custom-collection", vector_name: "custom-model", top_k: 25 };

    const result = applyAudioGraphTemplate(first, "meeting_complete", [{
      collection_name: "other",
      vectors: [{ name: "other-model", size: 192, distance: "cosine" }],
      template: null,
    }]);

    expect(result.nodes.find((node) => node.id === "audio-embedding")).toMatchObject({
      engine_id: "embedding-1",
      config: { algorithm: "custom-model", expected_dimension: 384, normalize: false },
    });
    expect(result.nodes.find((node) => node.id === "vector-search")).toMatchObject({
      config: { collection_name: "custom-collection", vector_name: "custom-model", top_k: 25 },
    });
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
