import { describe, expect, it } from "vitest";

import {
  isResponseTextDelta,
  isTtsFirstAudio,
  appendGroupedEvent,
  realtimeSessionUpdate,
} from "./RealtimeTestPanel";

describe("realtimeSessionUpdate", () => {
  it("uses a transcription session for transcription workflows", () => {
    expect(realtimeSessionUpdate("transcription")).toEqual({
      type: "session.update",
      session: { type: "transcription" },
    });
  });

  it("uses a realtime session for voice-agent workflows", () => {
    expect(realtimeSessionUpdate("voice_agent")).toEqual({
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
