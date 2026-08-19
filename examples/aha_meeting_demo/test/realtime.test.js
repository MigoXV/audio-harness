import assert from "node:assert/strict";
import test from "node:test";

import { createLifecycle, finishAudioInput, realtimeURL } from "../src/realtime.js";

const config = { baseURL: "https://audio.example.com/v1", model: "meeting-transcribe" };

test("realtime URL negotiates transcription and AHA v1", () => {
  assert.equal(realtimeURL(config).toString(), "wss://audio.example.com/v1/realtime?intent=transcription&x_aha=v1");
});

test("mock upstream lifecycle only configures after created and only starts after updated", () => {
  const lifecycle = createLifecycle(config);
  assert.deepEqual(lifecycle.handle({ type: "session.updated" }), [{ type: "relay", event: { type: "session.updated" } }]);
  const [configuration] = lifecycle.handle({ type: "session.created" });
  assert.equal(configuration.type, "send_config");
  assert.equal(configuration.event.session.audio.input.format.rate, 16_000);
  assert.equal(configuration.event.session.audio.input.transcription.model, "meeting-transcribe");
  assert.equal(configuration.event.session.audio.input.turn_detection.type, "server_vad");
  assert.deepEqual(lifecycle.handle({ type: "session.created" }), [{ type: "relay", event: { type: "session.created" } }]);
  assert.deepEqual(lifecycle.handle({ type: "session.updated" }), [{ type: "ready" }]);
  assert.equal(lifecycle.ready, true);
  assert.deepEqual(lifecycle.handle({ type: "error", error: { message: "bad config" } }), [{ type: "relay", event: { type: "error", error: { message: "bad config" } } }]);
});

test("finishing audio appends trailing silence before committing the workflow", () => {
  const messages = [];
  const upstream = { readyState: 1, send: (value) => messages.push(JSON.parse(value)) };
  finishAudioInput(upstream);

  assert.equal(messages.length, 21);
  assert.equal(messages.slice(0, 20).every((message) => message.type === "input_audio_buffer.append"), true);
  assert.equal(Buffer.from(messages[0].audio, "base64").length, 3_200);
  assert.equal(messages[20].type, "input_audio_buffer.commit");
  assert.match(messages[20].event_id, /^commit_/);
});
