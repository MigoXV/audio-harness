import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AudioHarnessApi } from "../api/types";
import {
  RealtimeTestPanel,
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

  it("uses only the official output-audio event for first-audio latency", () => {
    expect(isTtsFirstAudio("response.output_audio.delta")).toBe(true);
    expect(isTtsFirstAudio("response.output_text.delta")).toBe(false);
  });
});

describe("realtimeEndpoint", () => {
  it("selects transcription through intent and negotiates AHA explicitly", () => {
    expect(realtimeEndpoint("wss", "transcription", "semantic vad", true)).toBe(
      "/v1/realtime?intent=transcription&x_aha=v1",
    );
  });

  it("negotiates AHA explicitly for a voice agent WSS", () => {
    expect(realtimeEndpoint("wss", "voice_agent", "voice agent", true)).toBe(
      "/v1/realtime?model=voice%20agent&x_aha=v1",
    );
  });

  it("keeps the transcription protocol native when AHA is disabled", () => {
    expect(realtimeEndpoint("wss", "transcription", "native-client", false)).toBe(
      "/v1/realtime?intent=transcription",
    );
  });

  it("supports explicit AHA negotiation over WebRTC", () => {
    expect(realtimeEndpoint("webrtc", "transcription", "speech", true)).toBe(
      "/v1/realtime/calls?model=speech&x_aha=v1",
    );
    expect(realtimeEndpoint("webrtc", "voice_agent", "assistant", true)).toBe(
      "/v1/realtime/calls?model=assistant&x_aha=v1",
    );
  });

  it("keeps a voice agent native when AHA is disabled", () => {
    expect(realtimeEndpoint("wss", "voice_agent", "assistant", false)).toBe(
      "/v1/realtime?model=assistant",
    );
  });
});

describe("eventDetail", () => {
  it("omits encoded audio payloads from the event log", () => {
    expect(eventDetail({
      type: "response.output_audio.delta",
      delta: "A".repeat(10_000),
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

describe("RealtimeTestPanel", () => {
  const renderPanel = (workflowKind: "transcription" | "voice_agent") => render(createElement(RealtimeTestPanel, {
    api: {} as AudioHarnessApi,
    model: workflowKind === "transcription" ? "speech" : "assistant",
    workflowKind,
    canTest: true,
  }));

  it("defaults transcription tests to WSS and exposes AHA negotiation", () => {
    renderPanel("transcription");
    expect(screen.getByRole("button", { name: "WSS" })).toHaveClass("active");
    expect(screen.getByRole("checkbox", { name: "启用 AHA 扩展协议" })).toBeChecked();
  });

  it("defaults voice-agent tests to native WSS with optional AHA", () => {
    renderPanel("voice_agent");
    expect(screen.getByRole("button", { name: "WSS" })).toHaveClass("active");
    expect(screen.getByRole("button", { name: "WSS" })).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "启用 AHA 扩展协议" })).not.toBeChecked();
  });

  it("keeps WebRTC available as an explicit fallback", () => {
    renderPanel("voice_agent");
    fireEvent.click(screen.getByRole("button", { name: "WebRTC" }));
    expect(screen.getByRole("button", { name: "WebRTC" })).toHaveClass("active");
    expect(screen.getByRole("button", { name: "WSS" })).not.toHaveClass("active");
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
