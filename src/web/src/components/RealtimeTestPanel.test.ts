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
  nextTranscriptView,
  realtimeEndpoint,
  realtimeSessionUpdate,
  vectorSearchEventView,
  type TranscriptView,
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

  it("describes interim transcript snapshots and clearing", () => {
    expect(eventDetail({
      type: "x_aha.input_audio_transcription.interim",
      revision: 2,
      audio_start_ms: 0,
      audio_end_ms: 1500,
      transcript: "正在识别",
    })).toBe("预览 r2 · 0–1500 ms · 正在识别");
    expect(eventDetail({
      type: "x_aha.input_audio_transcription.interim_cleared",
      reason: "primary_transcript_started",
    })).toBe("中间结果已清除：primary_transcript_started");
  });

  it("summarizes completed and failed vector search events", () => {
    expect(eventDetail({
      type: "x_aha.vector.search.completed",
      collection_name: "voiceprints",
      vector_name: "eres2netv2",
      latency_ms: 12.5,
      groups: [{ speaker_id: "speaker-1" }, { speaker_id: "speaker-2" }],
    })).toBe("2 个候选人 · 12.5 ms");
    expect(eventDetail({
      type: "x_aha.vector.search.failed",
      collection_name: "voiceprints",
      vector_name: "eres2netv2",
      error: { code: "vector_search_failed", message: "Qdrant 不可用" },
    })).toBe("Qdrant 不可用");
  });
});

describe("vectorSearchEventView", () => {
  it("parses a completed push into displayable speaker groups", () => {
    expect(vectorSearchEventView({
      type: "x_aha.vector.search.completed",
      event_id: "event-1",
      item_id: "item-1",
      audio_start_ms: 100,
      audio_end_ms: 2300,
      collection_name: "voiceprints",
      vector_name: "eres2netv2",
      latency_ms: 18.25,
      groups: [{
        speaker_id: "speaker-1",
        score: 0.9321,
        observation: { point_id: "point-1", payload: { speaker_id: "speaker-1" } },
      }],
    }, "12:30:00")).toEqual({
      status: "completed",
      groups: [{ speakerId: "speaker-1", score: 0.9321 }],
      error: null,
    });
  });

  it("parses a failed push and ignores unrelated events", () => {
    expect(vectorSearchEventView({ type: "response.done" }, "12:30:00")).toBeNull();
    expect(vectorSearchEventView({
      type: "x_aha.vector.search.failed",
      event_id: "event-2",
      collection_name: "voiceprints",
      vector_name: "eres2netv2",
      error: { message: "Qdrant 不可用" },
    }, "12:31:00")).toMatchObject({
      status: "failed",
      groups: [],
      error: "Qdrant 不可用",
    });
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

describe("nextTranscriptView", () => {
  it("replaces the previous final result through the next turn lifecycle", () => {
    let view: TranscriptView = { phase: "final", text: "上一轮最终结果", itemId: "old" };

    view = nextTranscriptView(view, { type: "input_audio_buffer.speech_started", item_id: "new" });
    expect(view).toEqual({ phase: "listening", text: "正在聆听…", itemId: "new" });

    view = nextTranscriptView(view, {
      type: "x_aha.input_audio_transcription.interim",
      item_id: "new",
      transcript: "新的中间",
    });
    expect(view).toEqual({ phase: "interim", text: "新的中间", itemId: "new" });

    view = nextTranscriptView(view, {
      type: "x_aha.input_audio_transcription.interim_cleared",
      item_id: "new",
      reason: "primary_transcript_started",
    });
    expect(view).toEqual({ phase: "processing", text: "正在生成最终转写…", itemId: "new" });

    view = nextTranscriptView(view, {
      type: "conversation.item.input_audio_transcription.delta",
      item_id: "new",
      delta: "新的",
    });
    view = nextTranscriptView(view, {
      type: "conversation.item.input_audio_transcription.delta",
      item_id: "new",
      delta: "最终结果",
    });
    expect(view).toEqual({ phase: "primary", text: "新的最终结果", itemId: "new" });

    view = nextTranscriptView(view, {
      type: "conversation.item.input_audio_transcription.completed",
      item_id: "new",
      transcript: "新的最终结果。",
    });
    expect(view).toEqual({ phase: "final", text: "新的最终结果。", itemId: "new" });
  });

  it("keeps an interim result visible while speech is stopping", () => {
    const interim = { phase: "interim" as const, text: "还在识别", itemId: "item" };

    expect(nextTranscriptView(interim, {
      type: "input_audio_buffer.speech_stopped",
      item_id: "item",
    })).toEqual(interim);
    expect(nextTranscriptView({ phase: "listening", text: "正在聆听…", itemId: "item" }, {
      type: "input_audio_buffer.speech_stopped",
      item_id: "item",
    })).toEqual({ phase: "processing", text: "正在生成最终转写…", itemId: "item" });
  });

  it("does not hide an interim result that arrives before speech started", () => {
    const interim = nextTranscriptView(
      { phase: "final", text: "上一轮最终结果", itemId: "old" },
      {
        type: "x_aha.input_audio_transcription.interim",
        item_id: "new",
        transcript: "抢先到达的预览",
      },
    );

    expect(nextTranscriptView(interim, {
      type: "input_audio_buffer.speech_started",
      item_id: "new",
    })).toEqual({ phase: "interim", text: "抢先到达的预览", itemId: "new" });
  });

  it("ignores a completed result from an older item while a new turn is active", () => {
    const current: TranscriptView = {
      phase: "interim",
      text: "当前轮预览",
      itemId: "new",
    };

    expect(nextTranscriptView(current, {
      type: "conversation.item.input_audio_transcription.completed",
      item_id: "old",
      transcript: "迟到的上一轮最终结果",
    })).toEqual(current);
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
