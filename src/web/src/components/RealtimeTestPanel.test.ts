import { describe, expect, it } from "vitest";

import {
  isResponseTextDelta,
  isTtsFirstAudio,
  appendGroupedEvent,
  eventDetail,
  realtimeEndpoint,
  realtimeSessionUpdate,
} from "./RealtimeTestPanel";

describe("realtimeSessionUpdate", () => {
  it("uses a transcription session for transcription workflows", () => {
    expect(realtimeSessionUpdate("transcription", "gpt-4o-transcribe")).toEqual({
      type: "session.update",
      session: {
        type: "transcription",
        audio: {
          input: {
            transcription: { model: "gpt-4o-transcribe" },
          },
        },
      },
    });
  });

  it("uses a realtime session for voice-agent workflows", () => {
    expect(realtimeSessionUpdate("voice_agent", "assistant")).toEqual({
      type: "session.update",
      session: { type: "realtime" },
    });
  });

  it("recognizes both text and TTS transcript delta events", () => {
    expect(isResponseTextDelta("response.output_text.delta")).toBe(true);
    expect(isResponseTextDelta("response.output_audio_transcript.delta")).toBe(true);
    expect(isResponseTextDelta("response.output_audio.delta")).toBe(false);
  });

  it("recognizes both current and legacy first-TTS-audio events", () => {
    expect(isTtsFirstAudio("audio_harness.tts_first_audio")).toBe(true);
    expect(isTtsFirstAudio("response.output_audio.delta")).toBe(true);
    expect(isTtsFirstAudio("response.output_text.delta")).toBe(false);
  });
});

describe("realtimeEndpoint", () => {
  it("enables x_aha events for the WSS test connection", () => {
    expect(realtimeEndpoint("wss", "semantic vad", true)).toBe(
      "/v1/realtime?intent=transcription&x_aha=v1",
    );
  });

  it("enables x_aha events for the WebRTC test connection", () => {
    expect(realtimeEndpoint("webrtc", "voice-agent", true)).toBe(
      "/v1/realtime/calls?model=voice-agent&x_aha=v1",
    );
  });

  it("keeps the native protocol when the extension is disabled", () => {
    expect(realtimeEndpoint("wss", "native-client", false)).toBe(
      "/v1/realtime?intent=transcription",
    );
  });
});

describe("eventDetail", () => {
  it("omits encoded audio payloads from the event log", () => {
    expect(eventDetail({
      type: "response.output_audio.delta",
      delta: "A".repeat(10_000),
    })).toBe("音频数据已省略");
    expect(eventDetail({
      type: "audio_harness.tts_first_audio",
      delta: "B".repeat(10_000),
    })).toBe("音频数据已省略");
  });

  it("describes a pending semantic turn after the physical pause", () => {
    expect(eventDetail({
      type: "x_aha.vad.physical_speech_stopped",
      audio_start_ms: 1200,
      audio_end_ms: 2380,
      turn_completed: false,
    })).toBe("物理停顿 1200–2380 ms；语义轮次未结束");
  });
});

describe("appendGroupedEvent", () => {
  const line = (id: string, type: string) => ({
    id,
    type,
    time: "12:00:00",
    detail: id,
    count: 1,
  });

  it("groups adjacent events of the same type and keeps the latest detail", () => {
    expect(appendGroupedEvent([line("one", "response.output_text.delta")], line("two", "response.output_text.delta"))).toEqual([
      { ...line("one", "response.output_text.delta"), detail: "two", count: 2 },
    ]);
  });

  it("starts a new row when another event type interrupts the sequence", () => {
    const first = appendGroupedEvent([], line("one", "a"));
    const second = appendGroupedEvent(first, line("two", "b"));
    expect(appendGroupedEvent(second, line("three", "a"))).toHaveLength(3);
  });
});
