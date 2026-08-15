import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AudioHarnessApi } from "../api/types";
import { VectorWorkspacePage } from "./VectorWorkspacePage";


const engine = (id: string, kind: "vad" | "embedding") => ({
  id,
  slug: id,
  name: id === "vad-1" ? "VAD-1" : "Embedding-1",
  kind,
  api: kind === "vad" ? "ux_vad" : "engine_feature",
  protocol: "grpc",
  target: "test:1",
  connect_timeout_seconds: 1,
  request_timeout_seconds: null,
  credential_env: null,
  api_key: null,
  enabled: true,
  status: "healthy",
  image: null,
  description: null,
  status_basis: null,
  status_observed_at: null,
  source: null,
  last_success_at: null,
  probe_latency_ms: null,
  consecutive_failures: 0,
  next_probe_at: null,
  details: {},
  revision: 1,
  created_at: "2026-08-15T00:00:00Z",
  updated_at: "2026-08-15T00:00:00Z",
});

function fakeApi() {
  const previewVectorSearchVad = vi.fn().mockResolvedValue({
    audio_base64: btoa("RIFF-preview"),
    content_type: "audio/wav",
    duration_ms: 1250,
  });
  const searchVectorAudio = vi.fn().mockResolvedValue({
    embedding: [1, 0],
    groups: [{
      speaker_id: "speaker-1",
      score: 0.9321,
      observation: {
        point_id: "point-secret",
        payload: { secret: "payload-secret" },
      },
    }],
  });
  const registerVectorAudio = vi.fn().mockResolvedValue({ point_id: "point-1" });
  const api = {
    listVectorSpaces: vi.fn().mockResolvedValue([{
      collection_name: "voiceprints",
      vectors: [{ name: "eres2netv2", size: 192, distance: "cosine" }],
      template: "voiceprint",
    }]),
    listEngines: vi.fn().mockImplementation(async ({ kind }) => ({
      items: kind === "vad" ? [engine("vad-1", "vad")] : [engine("embedding-1", "embedding")],
      total: 1,
      limit: 100,
      offset: 0,
    })),
    getVectorInfrastructure: vi.fn().mockResolvedValue({
      configured: true,
      ready: true,
      grpc_target: "qdrant:6334",
      http_url: null,
    }),
    previewVectorSearchVad,
    searchVectorAudio,
    registerVectorAudio,
  } as unknown as AudioHarnessApi;
  return { api, previewVectorSearchVad, searchVectorAudio, registerVectorAudio };
}

class FakeMediaRecorder {
  static latest: FakeMediaRecorder | null = null;
  state: RecordingState = "inactive";
  mimeType = "audio/webm";
  ondataavailable: ((event: BlobEvent) => void) | null = null;
  onstop: (() => void) | null = null;

  constructor(stream: MediaStream) {
    void stream;
    FakeMediaRecorder.latest = this;
  }

  start() {
    this.state = "recording";
  }

  stop() {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["recorded"], { type: this.mimeType }) } as BlobEvent);
    this.onstop?.();
  }
}

describe("VectorWorkspacePage", () => {
  beforeEach(() => {
    let objectUrl = 0;
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => `blob:test-${objectUrl += 1}`),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    FakeMediaRecorder.latest = null;
  });

  it("automatically processes VAD audio and uploads the exact result to speaker search", async () => {
    const { api, previewVectorSearchVad, searchVectorAudio } = fakeApi();
    const { container } = render(<VectorWorkspacePage api={api} refreshVersion={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "人物搜索" }));

    expect(screen.getByLabelText("VAD 预处理")).toHaveValue("vad-1");
    const source = new File(["original"], "source.wav", { type: "audio/wav" });
    const dropzone = screen.getByText("拖放音频到这里").closest("label");
    fireEvent.drop(dropzone!, { dataTransfer: { files: [source] } });
    expect(screen.getByRole("button", { name: "执行人物搜索" })).toBeDisabled();

    await waitFor(() => expect(previewVectorSearchVad).toHaveBeenCalledTimes(1));
    expect(screen.getByText(/00:01 · 搜索将使用此音频/)).toBeVisible();
    expect(screen.getByLabelText("VAD 裁剪后音频")).toBeVisible();
    const previewForm = previewVectorSearchVad.mock.calls[0][1] as FormData;
    expect(previewForm.get("audio")).toBe(source);
    expect(previewForm.get("vad_engine_id")).toBe("vad-1");

    fireEvent.click(screen.getByRole("button", { name: "执行人物搜索" }));
    await waitFor(() => expect(searchVectorAudio).toHaveBeenCalledTimes(1));
    const searchForm = searchVectorAudio.mock.calls[0][1] as FormData;
    const searchedAudio = searchForm.get("audio") as File;
    expect(searchedAudio.name).toBe("vad-preview.wav");
    expect(await searchedAudio.text()).toBe("RIFF-preview");
    expect(await screen.findByText("speaker-1")).toBeVisible();
    expect(screen.getByText("分数 0.9321")).toBeVisible();
    expect(screen.queryByText("point-secret")).not.toBeInTheDocument();
    expect(screen.queryByText("payload-secret")).not.toBeInTheDocument();
    expect(container.querySelector(".vector-speaker-result pre")).toBeNull();

    fireEvent.change(screen.getByLabelText("VAD 预处理"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "执行人物搜索" }));
    await waitFor(() => expect(searchVectorAudio).toHaveBeenCalledTimes(2));
    const originalSearchForm = searchVectorAudio.mock.calls[1][1] as FormData;
    expect(originalSearchForm.get("audio")).toBe(source);
  });

  it("automatically uses the VAD result for registration too", async () => {
    const { api, previewVectorSearchVad, registerVectorAudio } = fakeApi();
    render(<VectorWorkspacePage api={api} refreshVersion={0} />);
    const source = new File(["original"], "source.wav", { type: "audio/wav" });
    const dropzone = await screen.findByText("拖放音频到这里");
    fireEvent.drop(dropzone.closest("label")!, { dataTransfer: { files: [source] } });
    await waitFor(() => expect(previewVectorSearchVad).toHaveBeenCalledTimes(1));
    expect(screen.getByText(/00:01 · 登记将使用此音频/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("Speaker ID"), { target: { value: "speaker-1" } });
    fireEvent.change(screen.getByLabelText("Channel ID"), { target: { value: "mic-1" } });
    fireEvent.click(screen.getByRole("button", { name: "生成并登记" }));
    await waitFor(() => expect(registerVectorAudio).toHaveBeenCalledTimes(1));
    const registrationForm = registerVectorAudio.mock.calls[0][1] as FormData;
    const registrationAudio = registrationForm.get("audio") as File;
    expect(registrationAudio.name).toBe("vad-preview.wav");
    expect(await registrationAudio.text()).toBe("RIFF-preview");
    expect(registrationForm.get("cluster_id")).toBeNull();
    expect(previewVectorSearchVad).toHaveBeenCalledTimes(1);
  });

  it("shows recording duration and releases the microphone when stopped", async () => {
    const stopTrack = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: stopTrack }], getAudioTracks: () => [{ label: "Built-in Microphone" }] }) },
    });
    Object.defineProperty(globalThis, "MediaRecorder", {
      configurable: true,
      value: FakeMediaRecorder,
    });
    const { api } = fakeApi();
    render(<VectorWorkspacePage api={api} refreshVersion={0} />);
    const sourceTab = await screen.findByRole("button", { name: "现场录音" });
    fireEvent.click(sourceTab);
    expect(screen.queryByText("拖放音频到这里")).not.toBeInTheDocument();
    const recordButton = screen.getByRole("button", { name: "开始录音" });
    vi.useFakeTimers();

    await act(async () => { fireEvent.click(recordButton); });
    expect(screen.getByLabelText("Channel ID")).toHaveValue("Built-in Microphone");
    await act(async () => { vi.advanceTimersByTime(1250); });
    expect(screen.getByText("00:01")).toBeVisible();
    act(() => { fireEvent.click(screen.getByRole("button", { name: "停止录音" })); });

    expect(screen.getByLabelText("原始音频")).toBeVisible();
    expect(screen.getByText(/录音 00:01/)).toBeVisible();
    expect(stopTrack).toHaveBeenCalledTimes(1);
  });
});
