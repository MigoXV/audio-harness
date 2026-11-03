import type { AudioNode, AudioWorkflowDocument, EngineAsset, VectorSpace } from "../api/types";

export type AudioGraphTemplateId =
  | "physical_asr"
  | "physical_semantic_asr"
  | "dual_physical_asr"
  | "dual_physical_semantic_asr"
  | "voice_agent"
  | "meeting_complete"
  | "meeting_streaming";

type NodeKind = AudioNode["kind"];

function existingNode<T extends NodeKind>(
  document: AudioWorkflowDocument,
  kind: T,
  preferredId: string,
): Extract<AudioNode, { kind: T }> | undefined {
  return document.nodes.find((node) => node.kind === kind && node.id === preferredId) as Extract<AudioNode, { kind: T }> | undefined
    ?? document.nodes.find((node) => node.kind === kind) as Extract<AudioNode, { kind: T }> | undefined;
}

function outputNode<T extends NodeKind>(
  document: AudioWorkflowDocument,
  outputName: string,
  kind: T,
): Extract<AudioNode, { kind: T }> | undefined {
  const binding = document.outputs.find((output) => output.name === outputName);
  const node = binding
    ? document.nodes.find((candidate) => candidate.id === binding.source.node_id)
    : undefined;
  return node?.kind === kind ? node as Extract<AudioNode, { kind: T }> : undefined;
}

function physicalVad(document: AudioWorkflowDocument) {
  const existing = outputNode(document, "speech_started", "physical_vad")
    ?? existingNode(document, "physical_vad", "physical-vad");
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
        speech_chunk_seconds: 1,
        speech_chunk_holdback_seconds: 1,
        progress_interval_seconds: 0.2,
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
  const existing = outputNode(document, "primary_transcript", "asr")
    ?? existingNode(document, "asr", "asr");
  return existing
    ? { ...existing, id: "asr", config: { remove_punctuation: false, ...existing.config } }
    : { id: "asr", kind: "asr" as const, engine_id: null, config: { remove_punctuation: false } };
}


function interimAsr(document: AudioWorkflowDocument, primary: ReturnType<typeof asr>) {
  const existing = outputNode(document, "interim_transcript", "asr")
    ?? document.nodes.find((node): node is Extract<AudioNode, { kind: "asr" }> =>
      node.kind === "asr" && node.id === "interim-asr",
    );
  return existing
    ? { ...existing, id: "interim-asr", config: { remove_punctuation: true, ...existing.config } }
    : { ...primary, id: "interim-asr", config: { ...primary.config, remove_punctuation: true } };
}

function speakerAsr(document: AudioWorkflowDocument, primary: ReturnType<typeof asr>) {
  const existing = outputNode(document, "speaker_transcript", "asr")
    ?? document.nodes.find((node): node is Extract<AudioNode, { kind: "asr" }> =>
      node.kind === "asr" && node.id === "speaker-asr",
    );
  return existing
    ? { ...existing, id: "speaker-asr", config: { remove_punctuation: false, ...existing.config } }
    : { ...primary, id: "speaker-asr", config: { ...primary.config } };
}

function interimPhysicalVad(
  document: AudioWorkflowDocument,
  primary: ReturnType<typeof physicalVad>,
) {
  const boundPreviewAsr = outputNode(document, "interim_transcript", "asr");
  const previewAsrId = boundPreviewAsr?.id ?? "interim-asr";
  const previewEdge = document.edges.find((edge) =>
    edge.target.node_id === previewAsrId
    && edge.target.port === "segments"
    && edge.source.kind === "node",
  );
  const previewSource = previewEdge?.source;
  const bound = previewSource?.kind === "node"
    ? document.nodes.find((node) => node.id === previewSource.node_id && node.kind === "physical_vad")
    : undefined;
  const preferred = document.nodes.find((node) =>
    node.kind === "physical_vad" && node.id === "interim-physical-vad",
  );
  const existing = bound?.kind === "physical_vad"
    ? bound
    : preferred?.kind === "physical_vad" ? preferred : undefined;
  if (existing) return { ...existing, id: "interim-physical-vad" };
  return {
    id: "interim-physical-vad",
    kind: "physical_vad" as const,
    engine_id: primary.engine_id,
    config: {
      min_segment_seconds: 0.2,
      max_segment_seconds: 2,
      audio_buffer_seconds: 10,
      speech_pre_roll_seconds: 0,
      speech_chunk_seconds: 1,
      speech_chunk_holdback_seconds: 1,
      progress_interval_seconds: 0.2,
      postprocessing: {
        processor: "firered.stream_vad" as const,
        version: 1 as const,
        parameters: {
          smooth_window_size: 3,
          speech_threshold: 0.35,
          pad_start_frame: 3,
          min_speech_frame: 5,
          max_speech_frame: 200,
          min_silence_frame: 10,
        },
      },
    },
  };
}

function audioEmbedding(document: AudioWorkflowDocument, vectorSpace?: VectorSpace) {
  const existing = existingNode(document, "audio_embedding", "audio-embedding");
  if (existing) return { ...existing, id: "audio-embedding" };
  const vector = vectorSpace?.vectors[0];
  return {
    id: "audio-embedding",
    kind: "audio_embedding" as const,
    engine_id: null,
    config: {
      algorithm: vector?.name ?? "eres2netv2",
      expected_dimension: vector?.size ?? 192,
      normalize: true,
    },
  };
}

function vectorSearch(document: AudioWorkflowDocument, vectorSpace?: VectorSpace) {
  const existing = outputNode(document, "vector_search", "vector_search")
    ?? existingNode(document, "vector_search", "vector-search");
  if (existing) return { ...existing, id: "vector-search" };
  return {
    id: "vector-search",
    kind: "vector_search" as const,
    engine_id: null,
    config: {
      collection_name: vectorSpace?.collection_name ?? "voiceprints",
      vector_name: vectorSpace?.vectors[0]?.name ?? "eres2netv2",
      top_k: 10,
      score_threshold: null,
      filter: {},
      include_embedding: false,
    },
  };
}

function speakerFilter(document: AudioWorkflowDocument) {
  const existing = outputNode(document, "speaker_filter", "speaker_filter")
    ?? existingNode(document, "speaker_filter", "speaker-filter");
  if (existing) return { ...existing, id: "speaker-filter" };
  return {
    id: "speaker-filter",
    kind: "speaker_filter" as const,
    engine_id: null,
    config: { allowed_speaker_ids: ["speaker-001"], min_score: 0.75 },
  };
}

function speakerDiarization(document: AudioWorkflowDocument) {
  const existing = outputNode(
    document,
    "diarization_local",
    "speaker_diarization",
  ) ?? existingNode(document, "speaker_diarization", "speaker-diarization");
  if (existing) return { ...existing, id: "speaker-diarization" };
  return {
    id: "speaker-diarization",
    kind: "speaker_diarization" as const,
    engine_id: null,
    config: {
      min_batch_seconds: 60,
      max_batch_seconds: 120,
      min_asr_segment_seconds: 0.5,
      postprocessing: {
        processor: "audio_harness.diarization.vad_coverage" as const,
        version: 1 as const,
      },
    },
  };
}

function speakerClustering(
  document: AudioWorkflowDocument,
  engines: readonly EngineAsset[],
) {
  const existing = outputNode(
    document,
    "diarization_cluster",
    "speaker_clustering",
  ) ?? existingNode(document, "speaker_clustering", "speaker-clustering");
  if (existing) return { ...existing, id: "speaker-clustering" };
  const engine = engines.find((item) =>
    item.enabled
    && (item.api === "engine_feature" || item.api === "dummy_clustering"),
  );
  return {
    id: "speaker-clustering",
    kind: "speaker_clustering" as const,
    engine_id: engine?.id ?? null,
    config: {
      embedding_algorithm: "eres2netv2",
      expected_dimension: 192,
      normalize: true,
      max_embedding_seconds: 60,
      clustering_algorithm: "hdbscan" as const,
      parameters: {
        min_cluster_size: 2,
        min_samples: 2,
        cluster_selection_epsilon: 0,
        allow_single_cluster: false,
      },
      postprocessing: {
        processor: "audio_harness.speaker_clustering.complete_link_merge" as const,
        version: 1 as const,
        high_similarity_threshold: 0.70,
        low_similarity_threshold: 0.50,
      },
      max_vectors: 10000,
    },
    failure_policy: "fail_open" as const,
  };
}

export function applyAudioGraphTemplate(
  document: AudioWorkflowDocument,
  template: AudioGraphTemplateId,
  vectorSpaces: readonly VectorSpace[] = [],
  engines: readonly EngineAsset[] = [],
): AudioWorkflowDocument {
  if (template === "meeting_streaming") {
    const base = applyAudioGraphTemplate(document, "meeting_complete", vectorSpaces, engines);
    const existing = existingNode(document, "streaming_speaker_diarization", "streaming-speaker-diarization");
    const node: Extract<AudioNode, { kind: "streaming_speaker_diarization" }> = {
      id: "streaming-speaker-diarization", kind: "streaming_speaker_diarization",
      engine_id: existing?.engine_id ?? engines.find((engine) => engine.enabled && engine.api === "ux_speaker_diarization_duplex")?.id ?? null,
      config: { input_buffer_latency_ms: 320, min_asr_segment_seconds: 0.5, max_pending_audio_seconds: 120, max_segments: 10000, ...existing?.config },
    };
    const removed = new Set(["speaker-diarization", "speaker-clustering"]);
    return {
      ...base,
      nodes: [...base.nodes.filter((value) => !removed.has(value.id)), node],
      edges: [
        ...base.edges.filter((edge) => !removed.has(edge.target.node_id) && !(edge.source.kind === "node" && removed.has(edge.source.node_id))),
        { id: "audio-to-streaming-diarization", source: { kind: "input", name: "audio" }, target: { node_id: node.id, port: "audio" } },
        { id: "speech-chunks-to-streaming-diarization", source: { kind: "node", node_id: "physical-vad", port: "speech_chunks" }, target: { node_id: node.id, port: "segments" } },
        { id: "streaming-diarization-to-asr", source: { kind: "node", node_id: node.id, port: "segments" }, target: { node_id: "speaker-asr", port: "segments" } },
      ],
      outputs: base.outputs.map((output) => output.name === "diarization_local"
        ? { ...output, source: { kind: "node", node_id: node.id, port: "local_results" } }
        : output.name === "diarization_cluster"
          ? { ...output, source: { kind: "node", node_id: node.id, port: "cluster_results" } } : output),
      layout: { ...Object.fromEntries(Object.entries(base.layout ?? {}).filter(([id]) => !removed.has(id))), [node.id]: { x: 800, y: 400 } },
    };
  }
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

  if (template === "dual_physical_asr") {
    const previewRecognition = interimAsr(document, recognition);
    const previewPhysical = interimPhysicalVad(
      document,
      physical,
    );
    return {
      schema_version: 1,
      nodes: [physical, recognition, previewPhysical, previewRecognition],
      edges: [
        { id: "audio-to-physical-vad", source: { kind: "input", name: "audio" }, target: { node_id: "physical-vad", port: "audio" } },
        { id: "physical-vad-to-asr", source: { kind: "node", node_id: "physical-vad", port: "segments" }, target: { node_id: "asr", port: "segments" } },
        { id: "audio-to-interim-physical-vad", source: { kind: "input", name: "audio" }, target: { node_id: "interim-physical-vad", port: "audio" } },
        { id: "interim-physical-vad-to-asr", source: { kind: "node", node_id: "interim-physical-vad", port: "segments" }, target: { node_id: "interim-asr", port: "segments" } },
      ],
      outputs: [
        { name: "primary_transcript", source: { kind: "node", node_id: "asr", port: "transcripts" } },
        { name: "interim_transcript", source: { kind: "node", node_id: "interim-asr", port: "transcripts" } },
        { name: "speech_started", source: { kind: "node", node_id: "physical-vad", port: "speech_started" } },
        { name: "speech_stopped", source: { kind: "node", node_id: "physical-vad", port: "speech_stopped" } },
      ],
      layout: {
        "physical-vad": { x: 240, y: 40 },
        asr: { x: 520, y: 40 },
        "interim-physical-vad": { x: 240, y: 360 },
        "interim-asr": { x: 520, y: 360 },
      },
    };
  }

  const semantic = semanticVad(document);
  if (template === "voice_agent") {
    const preferredVectorSpace = vectorSpaces.find((space) => space.template === "voiceprint");
    const embedding = audioEmbedding(document, preferredVectorSpace);
    const search = vectorSearch(document, preferredVectorSpace);
    const filter = speakerFilter(document);
    return {
      schema_version: 1,
      nodes: [physical, semantic, embedding, search, filter, recognition],
      edges: [
        { id: "audio-to-physical-vad", source: { kind: "input", name: "audio" }, target: { node_id: "physical-vad", port: "audio" } },
        { id: "physical-vad-to-semantic-vad", source: { kind: "node", node_id: "physical-vad", port: "segments" }, target: { node_id: "semantic-vad", port: "segments" } },
        { id: "semantic-vad-to-audio-embedding", source: { kind: "node", node_id: "semantic-vad", port: "segments" }, target: { node_id: "audio-embedding", port: "segments" } },
        { id: "audio-embedding-to-vector-search", source: { kind: "node", node_id: "audio-embedding", port: "embeddings" }, target: { node_id: "vector-search", port: "embeddings" } },
        { id: "semantic-vad-to-speaker-filter", source: { kind: "node", node_id: "semantic-vad", port: "segments" }, target: { node_id: "speaker-filter", port: "segments" } },
        { id: "vector-search-to-speaker-filter", source: { kind: "node", node_id: "vector-search", port: "results" }, target: { node_id: "speaker-filter", port: "results" } },
        { id: "speaker-filter-to-asr", source: { kind: "node", node_id: "speaker-filter", port: "segments" }, target: { node_id: "asr", port: "segments" } },
      ],
      outputs: [
        { name: "primary_transcript", source: { kind: "node", node_id: "asr", port: "transcripts" } },
        { name: "speech_started", source: { kind: "node", node_id: "speaker-filter", port: "speech_started" } },
        { name: "speech_stopped", source: { kind: "node", node_id: "speaker-filter", port: "speech_stopped" } },
        { name: "turn_progress", source: { kind: "node", node_id: "semantic-vad", port: "turn_progress" } },
        { name: "vector_search", source: { kind: "node", node_id: "vector-search", port: "results" } },
        { name: "speaker_filter", source: { kind: "node", node_id: "speaker-filter", port: "decisions" } },
      ],
      layout: {
        "physical-vad": { x: 120, y: 160 },
        "semantic-vad": { x: 400, y: 160 },
        "audio-embedding": { x: 680, y: 40 },
        "vector-search": { x: 960, y: 40 },
        "speaker-filter": { x: 1240, y: 160 },
        asr: { x: 1520, y: 160 },
      },
    };
  }
  if (template === "dual_physical_semantic_asr" || template === "meeting_complete") {
    const previewRecognition = interimAsr(document, recognition);
    const previewPhysical = interimPhysicalVad(document, physical);
    const preferredVectorSpace = vectorSpaces.find((space) => space.template === "voiceprint");
    const embedding = audioEmbedding(document, preferredVectorSpace);
    const search = vectorSearch(document, preferredVectorSpace);
    const diarization = speakerDiarization(document);
    const speakerRecognition = speakerAsr(document, recognition);
    const clustering = speakerClustering(document, engines);
    const completeMeeting = template === "meeting_complete";
    return {
      schema_version: 1,
      nodes: [
        physical,
        semantic,
        recognition,
        previewPhysical,
        previewRecognition,
        ...(completeMeeting ? [embedding, search, diarization, speakerRecognition, clustering] : []),
      ],
      edges: [
        { id: "audio-to-physical-vad", source: { kind: "input", name: "audio" }, target: { node_id: "physical-vad", port: "audio" } },
        { id: "physical-vad-to-semantic-vad", source: { kind: "node", node_id: "physical-vad", port: "segments" }, target: { node_id: "semantic-vad", port: "segments" } },
        { id: "semantic-vad-to-asr", source: { kind: "node" as const, node_id: "semantic-vad", port: "segments" }, target: { node_id: "asr", port: "segments" } },
        ...(completeMeeting ? [
          { id: "semantic-vad-to-speaker-diarization", source: { kind: "node" as const, node_id: "semantic-vad", port: "segments" }, target: { node_id: "speaker-diarization", port: "segments" } },
          { id: "speaker-diarization-to-speaker-asr", source: { kind: "node" as const, node_id: "speaker-diarization", port: "segments" }, target: { node_id: "speaker-asr", port: "segments" } },
          { id: "speaker-diarization-to-clustering", source: { kind: "node" as const, node_id: "speaker-diarization", port: "segments" }, target: { node_id: "speaker-clustering", port: "segments" } },
        ] : []),
        { id: "audio-to-interim-physical-vad", source: { kind: "input", name: "audio" }, target: { node_id: "interim-physical-vad", port: "audio" } },
        { id: "interim-physical-vad-to-asr", source: { kind: "node", node_id: "interim-physical-vad", port: "segments" }, target: { node_id: "interim-asr", port: "segments" } },
        ...(completeMeeting ? [
          { id: "semantic-vad-to-audio-embedding", source: { kind: "node" as const, node_id: "semantic-vad", port: "segments" }, target: { node_id: "audio-embedding", port: "segments" } },
          { id: "audio-embedding-to-vector-search", source: { kind: "node" as const, node_id: "audio-embedding", port: "embeddings" }, target: { node_id: "vector-search", port: "embeddings" } },
        ] : []),
      ],
      outputs: [
        { name: "primary_transcript", source: { kind: "node", node_id: "asr", port: "transcripts" } },
        { name: "interim_transcript", source: { kind: "node", node_id: "interim-asr", port: "transcripts" } },
        { name: "speech_started", source: { kind: "node", node_id: "physical-vad", port: "speech_started" } },
        { name: "speech_stopped", source: { kind: "node", node_id: "semantic-vad", port: "speech_stopped" } },
        { name: "turn_progress", source: { kind: "node", node_id: "semantic-vad", port: "turn_progress" } },
        ...(completeMeeting ? [
          { name: "vector_search", source: { kind: "node" as const, node_id: "vector-search", port: "results" } },
          { name: "diarization_local", source: { kind: "node" as const, node_id: "speaker-diarization", port: "local_results" } },
          { name: "speaker_transcript", source: { kind: "node" as const, node_id: "speaker-asr", port: "speaker_transcripts" } },
          { name: "diarization_cluster", source: { kind: "node" as const, node_id: "speaker-clustering", port: "results" } },
        ] : []),
      ],
      layout: {
        "physical-vad": { x: 240, y: 40 },
        "semantic-vad": { x: 520, y: 40 },
        asr: { x: 800, y: 40 },
        "interim-physical-vad": { x: 240, y: 360 },
        "interim-asr": { x: 520, y: 360 },
        ...(completeMeeting ? {
          "audio-embedding": { x: 800, y: 240 },
          "vector-search": { x: 1080, y: 240 },
          "speaker-diarization": { x: 800, y: 400 },
          "speaker-asr": { x: 1080, y: 400 },
          "speaker-clustering": { x: 1080, y: 560 },
        } : {}),
      },
    };
  }

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
