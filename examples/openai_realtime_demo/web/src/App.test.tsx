import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { pcm16ToBase64, startMicrophone } from "./audio";

const microphone = vi.hoisted(() => ({ onChunk: undefined as
  | ((audio: string) => void)
  | undefined }));

vi.mock("./audio", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./audio")>();
  return {
    ...actual,
    startMicrophone: vi.fn(async (onChunk) => {
      microphone.onChunk = onChunk;
      return { stop: async () => undefined };
    }),
  };
});

class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly instances: MockWebSocket[] = [];
  readyState = MockWebSocket.OPEN;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn(() => this.onclose?.());

  constructor(url: string) {
    void url;
    MockWebSocket.instances.push(this);
  }

  receive(event: unknown) {
    this.onmessage?.({ data: JSON.stringify(event) } as MessageEvent<string>);
  }
}

describe("Realtime demo", () => {
  beforeEach(() => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    MockWebSocket.instances.length = 0;
    microphone.onChunk = undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          base_url: "https://api.openai.com/v1",
          model: "gpt-4o-transcribe",
          connection_mode: "intent",
        }),
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the fixed backend target without exposing a key", async () => {
    render(<App />);

    expect(
      screen.getByRole("heading", { name: "实时转写" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("https://api.openai.com/v1"),
    ).toBeInTheDocument();
    expect(screen.getByText("gpt-4o-transcribe")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("OPENAI_API_KEY");
    expect(
      screen.getByRole("button", { name: /协议事件/ }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("calculates and displays VAD processing progress lag from progress events", async () => {
    render(<App />);
    await screen.findByText("https://api.openai.com/v1");
    const startButton = screen.getByRole("button", { name: "打开麦克风" });
    fireEvent.click(startButton);
    fireEvent.click(startButton);

    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    const socket = MockWebSocket.instances[0];
    act(() => {
      socket.receive({ type: "session.created", event_id: "created" });
      socket.receive({ type: "session.updated", event_id: "updated" });
    });
    await waitFor(() => expect(startMicrophone).toHaveBeenCalledOnce());

    const chunk = pcm16ToBase64(new Int16Array(2_400));
    act(() => {
      microphone.onChunk?.(chunk);
      microphone.onChunk?.(chunk);
      socket.receive({
        type: "audio_harness.vad_progress",
        event_id: "vad-1",
        processed_audio_ms: 100,
      });
    });

    expect(screen.getByLabelText("VAD 处理进度滞后")).toHaveTextContent(
      "最近100 ms",
    );
    expect(screen.getByLabelText("VAD 处理进度滞后")).toHaveTextContent(
      "P50100 ms",
    );
    expect(screen.getByLabelText("VAD 处理进度滞后")).toHaveTextContent(
      "已处理100 ms",
    );
  });

  it("uses the server audio endpoint for end-to-completion latency", async () => {
    const now = vi.spyOn(performance, "now").mockReturnValue(1_000);
    render(<App />);
    await screen.findByText("https://api.openai.com/v1");
    fireEvent.click(screen.getByRole("button", { name: "打开麦克风" }));

    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    const socket = MockWebSocket.instances[0];
    act(() => {
      socket.receive({ type: "session.created", event_id: "created" });
      socket.receive({ type: "session.updated", event_id: "updated" });
    });
    await waitFor(() => expect(startMicrophone).toHaveBeenCalledOnce());

    const voiced = new Int16Array(2_400);
    voiced.fill(2_000);
    act(() => {
      microphone.onChunk?.(pcm16ToBase64(voiced));
      microphone.onChunk?.(pcm16ToBase64(new Int16Array(2_400)));
      socket.receive({
        type: "input_audio_buffer.speech_stopped",
        event_id: "stopped",
        item_id: "item-1",
        audio_end_ms: 150,
      });
    });

    now.mockReturnValue(1_100);
    act(() => {
      socket.receive({
        type: "conversation.item.input_audio_transcription.completed",
        event_id: "completed",
        item_id: "item-1",
        transcript: "测试完成",
      });
    });

    expect(screen.getByText("句末到完成").parentElement).toHaveTextContent(
      "50 ms",
    );
  });
});
