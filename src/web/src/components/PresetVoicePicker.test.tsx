import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { AudioHarnessApi, EngineAsset } from "../api/types";
import { PresetVoicePicker } from "./PresetVoicePicker";

const engine = { id: "tts-1", revision: 2, name: "Qwen TTS" } as EngineAsset;

function renderPicker(api: AudioHarnessApi, onAdd = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><PresetVoicePicker api={api} engine={engine} onAdd={onAdd} /></QueryClientProvider>);
  return onAdd;
}

describe("PresetVoicePicker", () => {
  it("filters the available Duplex catalog by case-insensitive name prefix", async () => {
    const api = { listEnginePresetVoices: vi.fn().mockResolvedValue({
      engine_id: engine.id, engine_revision: engine.revision, serving_mode: "duplex",
      items: [
        { voice_id: "rita", name: "Rita", language: "Chinese", supports_synthesize: true, supports_duplex: true, available_on_instance: true },
        { voice_id: "river", name: "River", language: "English", supports_synthesize: true, supports_duplex: true, available_on_instance: true },
        { voice_id: "offline", name: "Offline", language: "English", supports_synthesize: true, supports_duplex: false, available_on_instance: false },
      ],
    }) } as unknown as AudioHarnessApi;
    const onAdd = renderPicker(api);

    expect(await screen.findByText("已读取 2 个可用 Duplex 音色。")).toBeVisible();
    fireEvent.change(screen.getByLabelText("输入或搜索音色"), { target: { value: "rit" } });
    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rita/ })).toBeVisible();
      expect(screen.queryByRole("option", { name: /River/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("option", { name: /Offline/ })).not.toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /Rita/ }));
    expect(onAdd).toHaveBeenCalledWith("Rita");
  });

  it("keeps manual entry when the catalog cannot be read", async () => {
    const api = { listEnginePresetVoices: vi.fn().mockRejectedValue(new Error("unavailable")) } as unknown as AudioHarnessApi;
    const onAdd = renderPicker(api);

    expect(await screen.findByText(/仍可手工输入/)).toBeVisible();
    const input = screen.getByLabelText("输入或搜索音色");
    fireEvent.change(input, { target: { value: "manual-voice" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(onAdd).toHaveBeenCalledWith("manual-voice"));
  });
});
