import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { AudioHarnessApi, EngineAsset } from "../api/types";
import { EngineModelPicker } from "./EngineModelPicker";

const engine = {
  id: "llm-1",
  revision: 3,
  name: "Responses One",
} as EngineAsset;

function renderPicker(api: AudioHarnessApi, value: string, onChange = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><EngineModelPicker api={api} engine={engine} value={value} onChange={onChange} /></QueryClientProvider>);
  return onChange;
}

describe("EngineModelPicker", () => {
  it("automatically selects the sole discovered model", async () => {
    const api = { listEngineModels: vi.fn().mockResolvedValue({ engine_id: engine.id, engine_revision: engine.revision, items: ["only-model"] }) } as unknown as AudioHarnessApi;
    const onChange = renderPicker(api, "");

    await waitFor(() => expect(onChange).toHaveBeenCalledWith("only-model"));
    expect(screen.getByText("已从 Responses One 获取 1 个模型。")).toBeVisible();
  });

  it("keeps manual input available when discovery fails", async () => {
    const api = { listEngineModels: vi.fn().mockRejectedValue(new Error("provider unavailable")) } as unknown as AudioHarnessApi;
    const onChange = renderPicker(api, "");

    expect(await screen.findByText(/可继续手动输入/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("Responses 模型"), { target: { value: "manual-model" } });
    expect(onChange).toHaveBeenCalledWith("manual-model");
  });

  it("uses a themed searchable list instead of the native datalist", async () => {
    const api = { listEngineModels: vi.fn().mockResolvedValue({ engine_id: engine.id, engine_revision: engine.revision, items: ["chat-model", "reasoner-model"] }) } as unknown as AudioHarnessApi;
    const onChange = renderPicker(api, "");

    expect(await screen.findByText("已从 Responses One 获取 2 个模型。")).toBeVisible();
    const input = screen.getByRole("combobox", { name: "Responses 模型" });
    fireEvent.focus(input);
    expect(screen.getByRole("listbox", { name: "可用 Responses 模型" })).toBeVisible();
    expect(document.querySelector("datalist")).not.toBeInTheDocument();

    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("reasoner-model");
  });
});
