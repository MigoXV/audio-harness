export function createState() {
  return {
    turns: new Map(),
    batches: new Map(),
    segments: new Map(),
    vad: { processedAudioMs: 0, final: false },
    cluster: {
      revision: 0,
      lastEventRevision: 0,
      assignments: new Map(),
      lastError: null,
      final: false,
    },
    processing: { complete: false, failed: false, error: null },
  };
}

function turn(state, itemId) {
  if (!state.turns.has(itemId)) state.turns.set(itemId, { itemId, primary: "", interim: "", interimRevision: 0, final: false });
  return state.turns.get(itemId);
}

function segment(state, event) {
  if (!state.segments.has(event.segment_id)) {
    state.segments.set(event.segment_id, { segmentId: event.segment_id, speakerText: "", speakerFinal: false });
  }
  return state.segments.get(event.segment_id);
}

function replaceBatch(state, event, status) {
  const previous = state.batches.get(event.batch_id);
  if (previous?.status === "completed" && status !== "completed") return;
  if (status === "delta" && event.sequence != null && previous?.sequence != null && event.sequence <= previous.sequence) return;
  const segmentIds = [];
  for (const value of event.segments || []) {
    segmentIds.push(value.segment_id);
    Object.assign(segment(state, value), {
      ...value,
      batchId: event.batch_id,
      segmentId: value.segment_id,
    });
  }
  const currentSegmentIds = new Set(segmentIds);
  for (const previousId of previous?.segmentIds || []) {
    if (!currentSegmentIds.has(previousId) && state.segments.get(previousId)?.batchId === event.batch_id) {
      state.segments.delete(previousId);
    }
  }
  state.batches.set(event.batch_id, {
    sequence: event.sequence ?? previous?.sequence,
    status,
    completed: status === "completed",
    segmentIds,
    audioStartMs: event.batch_audio_start_ms ?? previous?.audioStartMs,
    audioEndMs: event.batch_audio_end_ms ?? previous?.audioEndMs,
    error: event.error ?? null,
  });
}

function number(value) {
  return Number.isFinite(value) ? value : 0;
}

function percent(value, total, complete = false) {
  if (complete) return 100;
  if (!(total > 0)) return 0;
  return Math.min(99.9, Math.max(0, value / total * 100));
}

export function getProgress(state, { sentBytes = 0, totalBytes = 0, totalAudioMs = 0 } = {}) {
  const completedBatchAudioMs = Math.max(0, ...[...state.batches.values()]
    .filter((batch) => batch.completed)
    .map((batch) => number(batch.audioEndMs)));
  return {
    send: Math.min(100, Math.max(0, totalBytes > 0 ? sentBytes / totalBytes * 100 : 0)),
    vad: percent(state.vad.processedAudioMs, totalAudioMs, state.vad.final),
    diarization: percent(completedBatchAudioMs, totalAudioMs, state.cluster.final),
    completedBatchAudioMs,
  };
}

function laneIdentity(item, assigned, clusterFinal) {
  if (assigned != null) {
    return {
      key: `cluster:${assigned}`,
      label: assigned >= 0 ? `说话人 ${assigned + 1}` : `说话人 ${assigned}`,
      colorKey: `cluster:${assigned}`,
      clustered: true,
      unassigned: false,
    };
  }
  const localId = item.local_speaker_id;
  if (!localId) return null;
  return {
    key: `local:${localId}`,
    label: `本地说话人 ${localId}`,
    colorKey: `local:${localId}`,
    clustered: false,
    unassigned: assigned === undefined && clusterFinal,
  };
}

export function buildSpeakerLanes(state) {
  const lanes = new Map();
  for (const item of state.segments.values()) {
    if (state.processing.complete && !item.speakerText && !item.speakerError) continue;
    const assigned = state.cluster.assignments.get(item.segmentId);
    const identity = laneIdentity(item, assigned, state.cluster.final);
    if (!identity) continue;
    if (!lanes.has(identity.key)) lanes.set(identity.key, { ...identity, segments: [] });
    const lane = lanes.get(identity.key);
    lane.unassigned ||= identity.unassigned;
    lane.segments.push(item);
  }
  for (const lane of lanes.values()) {
    lane.segments.sort((left, right) => number(left.audio_start_ms) - number(right.audio_start_ms));
    lane.audioStartMs = number(lane.segments[0]?.audio_start_ms);
  }
  return [...lanes.values()].sort((left, right) => left.audioStartMs - right.audioStartMs || left.key.localeCompare(right.key));
}

function joinSpeakerText(parts) {
  return parts.reduce((joined, part) => {
    if (!joined) return part;
    return /[A-Za-z0-9]$/.test(joined) && /^[A-Za-z0-9]/.test(part) ? `${joined} ${part}` : `${joined}${part}`;
  }, "");
}

export function buildSpeakerTranscript(state) {
  const entries = buildSpeakerLanes(state).flatMap((lane) => lane.segments.map((segment) => ({
    laneKey: lane.key,
    label: lane.label,
    colorKey: lane.colorKey,
    unassigned: lane.unassigned,
    segmentId: segment.segmentId,
    audioStartMs: number(segment.audio_start_ms),
    audioEndMs: number(segment.audio_end_ms),
    speakerText: segment.speakerText,
    speakerFinal: segment.speakerFinal,
    speakerError: segment.speakerError,
  }))).sort((left, right) => left.audioStartMs - right.audioStartMs || left.audioEndMs - right.audioEndMs);

  const merged = [];
  for (const entry of entries) {
    const previous = merged.at(-1);
    if (previous?.laneKey === entry.laneKey) {
      previous.audioEndMs = Math.max(previous.audioEndMs, entry.audioEndMs);
      previous.segmentIds.push(entry.segmentId);
      previous.parts.push(entry.speakerText);
      previous.pending ||= !entry.speakerFinal && !entry.speakerError;
      previous.error ||= entry.speakerError;
      continue;
    }
    merged.push({
      ...entry,
      segmentIds: [entry.segmentId],
      parts: [entry.speakerText],
      pending: !entry.speakerFinal && !entry.speakerError,
      error: entry.speakerError,
    });
  }
  return merged.map(({ parts, ...entry }) => ({ ...entry, text: joinSpeakerText(parts.filter(Boolean)) }));
}

export function colorIndex(colorKey, paletteSize) {
  let hash = 0;
  for (const character of colorKey) hash = (hash * 31 + character.codePointAt(0)) >>> 0;
  return paletteSize > 0 ? hash % paletteSize : 0;
}

export function resolveSelectedLane(lanes, selection) {
  if (!lanes.length) return null;
  if (selection?.segmentId) {
    const moved = lanes.find((lane) => lane.segments.some((item) => item.segmentId === selection.segmentId));
    if (moved) return moved;
  }
  return lanes.find((lane) => lane.key === selection?.laneKey) || lanes[0];
}

export function applyEvent(state, event) {
  switch (event.type) {
    case "conversation.item.input_audio_transcription.delta": {
      const value = turn(state, event.item_id);
      if (!value.final) value.primary += event.delta || "";
      break;
    }
    case "conversation.item.input_audio_transcription.completed": {
      const value = turn(state, event.item_id);
      value.primary = event.transcript || ""; value.interim = ""; value.final = true;
      break;
    }
    case "x_aha.input_audio_transcription.interim": {
      const value = turn(state, event.item_id);
      if (!value.final && event.revision > value.interimRevision) {
        value.interimRevision = event.revision; value.interim = event.transcript || "";
      }
      break;
    }
    case "x_aha.input_audio_transcription.interim_cleared": turn(state, event.item_id).interim = ""; break;
    case "x_aha.vad.progress":
      state.vad.processedAudioMs = Math.max(state.vad.processedAudioMs, number(event.processed_audio_ms));
      if (event.final) state.vad.final = true;
      break;
    case "x_aha.diarization.local.delta": replaceBatch(state, event, "delta"); break;
    case "x_aha.diarization.local.completed": replaceBatch(state, event, "completed"); break;
    case "x_aha.diarization.local.failed": replaceBatch(state, event, "failed"); break;
    case "x_aha.input_audio_transcription.speaker.delta": {
      const value = segment(state, event);
      Object.assign(value, {
        audio_start_ms: event.audio_start_ms ?? value.audio_start_ms,
        audio_end_ms: event.audio_end_ms ?? value.audio_end_ms,
        local_speaker_id: event.local_speaker_id ?? value.local_speaker_id,
      });
      if (!value.speakerFinal) value.speakerText += event.delta || "";
      break;
    }
    case "x_aha.input_audio_transcription.speaker.completed": {
      const value = segment(state, event);
      Object.assign(value, {
        audio_start_ms: event.audio_start_ms ?? value.audio_start_ms,
        audio_end_ms: event.audio_end_ms ?? value.audio_end_ms,
        local_speaker_id: event.local_speaker_id ?? value.local_speaker_id,
      });
      value.speakerText = event.transcript || ""; value.speakerFinal = true; value.speakerError = null;
      break;
    }
    case "x_aha.input_audio_transcription.speaker.failed": segment(state, event).speakerError = event.error; break;
    case "x_aha.diarization.cluster.completed":
      if (event.revision > state.cluster.lastEventRevision) {
        state.cluster.revision = event.revision;
        state.cluster.lastEventRevision = event.revision;
        state.cluster.assignments = new Map((event.assignments || []).map((item) => [item.segment_id, item.speaker_id]));
        state.cluster.lastError = null;
        if (event.final) {
          state.cluster.final = true;
          state.processing = { complete: true, failed: false, error: null };
        }
      }
      break;
    case "x_aha.diarization.cluster.failed":
      if (event.revision > state.cluster.lastEventRevision) {
        state.cluster.lastEventRevision = event.revision;
        state.cluster.lastError = event.error;
        if (event.final) {
          state.cluster.final = true;
          state.processing = { complete: true, failed: true, error: event.error };
        }
      }
      break;
    default: break;
  }
  return state;
}
