import assert from "node:assert/strict";
import test from "node:test";

import {
  applyEvent,
  buildSpeakerLanes,
  buildSpeakerTranscript,
  colorIndex,
  createState,
  getProgress,
  resolveSelectedLane,
} from "../web/state.js";

test("completed transcript replaces deltas and clears interim", () => {
  const state = createState();
  applyEvent(state, { type: "x_aha.input_audio_transcription.interim", item_id: "item", revision: 1, transcript: "临时" });
  applyEvent(state, { type: "conversation.item.input_audio_transcription.delta", item_id: "item", delta: "正式" });
  applyEvent(state, { type: "conversation.item.input_audio_transcription.completed", item_id: "item", transcript: "正式全文" });
  assert.deepEqual(state.turns.get("item"), { itemId: "item", primary: "正式全文", interim: "", interimRevision: 1, final: true });
});

test("local completed supersedes deltas and speaker completion supersedes deltas", () => {
  const state = createState();
  const segment = { segment_id: "seg", audio_start_ms: 0, audio_end_ms: 1_000, local_speaker_id: "batch:1" };
  applyEvent(state, { type: "x_aha.diarization.local.delta", batch_id: "batch", sequence: 1, segments: [segment] });
  applyEvent(state, { type: "x_aha.input_audio_transcription.speaker.delta", ...segment, delta: "你" });
  applyEvent(state, { type: "x_aha.diarization.local.completed", batch_id: "batch", sequence: 2, segments: [segment] });
  applyEvent(state, { type: "x_aha.input_audio_transcription.speaker.completed", ...segment, transcript: "你好" });
  applyEvent(state, { type: "x_aha.diarization.local.delta", batch_id: "batch", sequence: 3, segments: [] });
  assert.equal(state.batches.get("batch").completed, true);
  assert.equal(state.segments.get("seg").speakerText, "你好");
  assert.equal(state.segments.get("seg").speakerFinal, true);
});

test("local diarization snapshots remove superseded segments from the same batch", () => {
  const state = createState();
  const shared = { audio_start_ms: 0, audio_end_ms: 1_000, local_speaker_id: "batch:1" };
  applyEvent(state, { type: "x_aha.diarization.local.delta", batch_id: "batch", sequence: 1, segments: [{ ...shared, segment_id: "old" }] });
  applyEvent(state, { type: "x_aha.diarization.local.completed", batch_id: "batch", sequence: 2, segments: [{ ...shared, segment_id: "final" }] });
  assert.equal(state.segments.has("old"), false);
  assert.equal(state.segments.has("final"), true);
});

test("cluster completion replaces the complete assignment snapshot and failure preserves it", () => {
  const state = createState();
  applyEvent(state, { type: "x_aha.diarization.cluster.completed", revision: 1, assignments: [{ segment_id: "old", speaker_id: 0 }] });
  applyEvent(state, { type: "x_aha.diarization.cluster.completed", revision: 2, assignments: [{ segment_id: "new", speaker_id: 1 }] });
  applyEvent(state, { type: "x_aha.diarization.cluster.failed", revision: 3, error: { message: "失败" } });
  assert.equal(state.cluster.assignments.has("old"), false);
  assert.equal(state.cluster.assignments.get("new"), 1);
  assert.deepEqual(state.cluster.lastError, { message: "失败" });
});

test("progress is monotonic and only final events reach completion", () => {
  const state = createState();
  applyEvent(state, { type: "x_aha.vad.progress", processed_audio_ms: 4_000 });
  applyEvent(state, { type: "x_aha.vad.progress", processed_audio_ms: 3_000 });
  applyEvent(state, {
    type: "x_aha.diarization.local.completed",
    batch_id: "batch",
    batch_audio_start_ms: 1_000,
    batch_audio_end_ms: 7_500,
    segments: [],
  });
  assert.deepEqual(getProgress(state, { sentBytes: 500, totalBytes: 1_000, totalAudioMs: 10_000 }), {
    send: 50,
    vad: 40,
    diarization: 75,
    completedBatchAudioMs: 7_500,
  });

  applyEvent(state, { type: "x_aha.vad.progress", processed_audio_ms: 12_000, final: true });
  applyEvent(state, {
    type: "x_aha.diarization.cluster.completed",
    revision: 1,
    final: true,
    processed_audio_ms: 12_000,
    assignments: [],
  });
  const completed = getProgress(state, { sentBytes: 1_000, totalBytes: 1_000, totalAudioMs: 10_000 });
  assert.equal(completed.vad, 100);
  assert.equal(completed.diarization, 100);
  assert.deepEqual(state.processing, { complete: true, failed: false, error: null });
});

test("final cluster failure ends processing without discarding the last successful snapshot", () => {
  const state = createState();
  applyEvent(state, { type: "x_aha.diarization.cluster.completed", revision: 2, assignments: [{ segment_id: "seg", speaker_id: 0 }] });
  applyEvent(state, { type: "x_aha.diarization.cluster.failed", revision: 3, final: true, error: { message: "聚类失败" } });
  applyEvent(state, { type: "x_aha.diarization.cluster.completed", revision: 2, final: true, assignments: [] });
  assert.equal(state.cluster.assignments.get("seg"), 0);
  assert.equal(state.cluster.revision, 2);
  assert.equal(state.cluster.final, true);
  assert.deepEqual(state.processing, { complete: true, failed: true, error: { message: "聚类失败" } });
});

test("speaker lanes migrate all assigned segments by speaker ID", () => {
  const state = createState();
  const segments = [
    { segment_id: "early", audio_start_ms: 100, audio_end_ms: 400, local_speaker_id: "batch-a:1" },
    { segment_id: "merged", audio_start_ms: 500, audio_end_ms: 900, local_speaker_id: "batch-b:2" },
    { segment_id: "temporary", audio_start_ms: 1_000, audio_end_ms: 1_200, local_speaker_id: "batch-a:1" },
    { segment_id: "temporary-other", audio_start_ms: 1_300, audio_end_ms: 1_500, local_speaker_id: "batch-b:3" },
  ];
  applyEvent(state, { type: "x_aha.diarization.local.completed", batch_id: "batch", segments });
  let lanes = buildSpeakerLanes(state);
  assert.deepEqual(lanes.map((lane) => lane.key), ["local:batch-a:1", "local:batch-b:2", "local:batch-b:3"]);

  applyEvent(state, {
    type: "x_aha.diarization.cluster.completed",
    revision: 1,
    assignments: [
      { segment_id: "early", speaker_id: 0 },
      { segment_id: "merged", speaker_id: 0 },
      { segment_id: "temporary", speaker_id: -1 },
      { segment_id: "temporary-other", speaker_id: -2 },
    ],
  });
  lanes = buildSpeakerLanes(state);
  assert.deepEqual(lanes.map((lane) => lane.key), ["cluster:0", "cluster:-1", "cluster:-2"]);
  assert.deepEqual(lanes[0].segments.map((item) => item.segmentId), ["early", "merged"]);
  assert.deepEqual(lanes[1].segments.map((item) => item.segmentId), ["temporary"]);
  assert.deepEqual(lanes[2].segments.map((item) => item.segmentId), ["temporary-other"]);
});

test("speaker lanes hide fallback segments without local identity", () => {
  const state = createState();
  applyEvent(state, {
    type: "x_aha.input_audio_transcription.speaker.completed",
    segment_id: "gap",
    audio_start_ms: 100,
    audio_end_ms: 200,
    transcript: "补空白片段",
  });
  applyEvent(state, {
    type: "x_aha.diarization.local.completed",
    batch_id: "batch",
    segments: [{ segment_id: "speaker", audio_start_ms: 200, audio_end_ms: 500, local_speaker_id: "batch:1" }],
  });
  assert.deepEqual(buildSpeakerLanes(state).map((lane) => lane.key), ["local:batch:1"]);
  assert.equal(state.segments.get("gap").speakerText, "补空白片段");
});

test("final clustering marks genuine local speakers without assignments as unassigned", () => {
  const state = createState();
  applyEvent(state, {
    type: "x_aha.diarization.local.completed",
    batch_id: "batch",
    segments: [{ segment_id: "local", audio_start_ms: 100, audio_end_ms: 500, local_speaker_id: "batch:1" }],
  });
  applyEvent(state, { type: "x_aha.input_audio_transcription.speaker.completed", segment_id: "local", transcript: "有效转写" });
  assert.equal(buildSpeakerLanes(state)[0].unassigned, false);
  applyEvent(state, { type: "x_aha.diarization.cluster.completed", revision: 1, final: true, assignments: [] });
  const [lane] = buildSpeakerLanes(state);
  assert.equal(lane.unassigned, true);
});

test("all speaker transcript is time ordered and merges only adjacent entries from the same speaker", () => {
  const state = createState();
  applyEvent(state, {
    type: "x_aha.diarization.local.completed",
    batch_id: "batch",
    segments: [
      { segment_id: "a1", audio_start_ms: 0, audio_end_ms: 500, local_speaker_id: "batch:1" },
      { segment_id: "a2", audio_start_ms: 600, audio_end_ms: 900, local_speaker_id: "batch:1" },
      { segment_id: "b", audio_start_ms: 1_000, audio_end_ms: 1_200, local_speaker_id: "batch:2" },
      { segment_id: "a3", audio_start_ms: 1_300, audio_end_ms: 1_500, local_speaker_id: "batch:1" },
    ],
  });
  for (const [segmentId, transcript] of [["a1", "你好"], ["a2", "世界"], ["b", "收到"], ["a3", "再见"]]) {
    applyEvent(state, { type: "x_aha.input_audio_transcription.speaker.completed", segment_id: segmentId, transcript });
  }
  const entries = buildSpeakerTranscript(state);
  assert.deepEqual(entries.map((entry) => ({
    key: entry.laneKey,
    start: entry.audioStartMs,
    end: entry.audioEndMs,
    text: entry.text,
    segmentIds: entry.segmentIds,
  })), [
    { key: "local:batch:1", start: 0, end: 900, text: "你好世界", segmentIds: ["a1", "a2"] },
    { key: "local:batch:2", start: 1_000, end: 1_200, text: "收到", segmentIds: ["b"] },
    { key: "local:batch:1", start: 1_300, end: 1_500, text: "再见", segmentIds: ["a3"] },
  ]);
});

test("completed meetings hide local segments without a speaker ASR result", () => {
  const state = createState();
  applyEvent(state, {
    type: "x_aha.diarization.local.completed",
    batch_id: "batch",
    segments: [{ segment_id: "short", audio_start_ms: 100, audio_end_ms: 200, local_speaker_id: "batch:1" }],
  });
  assert.equal(buildSpeakerTranscript(state).length, 1);
  applyEvent(state, { type: "x_aha.diarization.cluster.completed", revision: 1, final: true, assignments: [] });
  assert.equal(buildSpeakerLanes(state).length, 0);
  assert.equal(buildSpeakerTranscript(state).length, 0);
});

test("lane selection follows an anchor segment across clustering revisions", () => {
  const state = createState();
  applyEvent(state, {
    type: "x_aha.diarization.local.completed",
    batch_id: "batch",
    segments: [{ segment_id: "seg", audio_start_ms: 100, audio_end_ms: 500, local_speaker_id: "batch:1" }],
  });
  const local = buildSpeakerLanes(state)[0];
  const selection = { laneKey: local.key, segmentId: "seg" };
  applyEvent(state, { type: "x_aha.diarization.cluster.completed", revision: 1, assignments: [{ segment_id: "seg", speaker_id: 4 }] });
  assert.equal(resolveSelectedLane(buildSpeakerLanes(state), selection).key, "cluster:4");
  assert.equal(colorIndex("cluster:4", 8), colorIndex("cluster:4", 8));
});
