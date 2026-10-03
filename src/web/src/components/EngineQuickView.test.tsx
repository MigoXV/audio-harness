import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { AudioHarnessApi, EngineAsset } from "../api/types";
import { EngineQuickView } from "./EngineQuickView";

const asset: EngineAsset = {
  id: "asr-1", slug: "asr-main", name: "主 ASR 引擎", kind: "asr", api: "ux_speech", protocol: "grpc", target: "localhost:50051",
  connect_timeout_seconds: 10, request_timeout_seconds: null, credential_env: null, api_key: null, enabled: true, status: "healthy", image: null,
  description: null, status_basis: "探测成功", status_observed_at: "2026-10-01T00:00:00Z", last_success_at: null, probe_latency_ms: 12,
  consecutive_failures: 0, next_probe_at: null, source: null, details: {}, revision: 1, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z",
};
function setup(getEngine = vi.fn().mockResolvedValue(asset), probeEngine = vi.fn().mockResolvedValue({}), engineId: string | null = asset.id) {
  const api = { getEngine, probeEngine } as unknown as AudioHarnessApi;
  const onUpdated = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter><EngineQuickView api={api} engineId={engineId} onUpdated={onUpdated} /></MemoryRouter></QueryClientProvider>);
  return { getEngine, probeEngine, onUpdated };
}

describe("engine quick view", () => {
  it("does not request details until opened and restores focus after Escape", async () => {
    const { getEngine, onUpdated } = setup();
    expect(getEngine).not.toHaveBeenCalled();
    const trigger = screen.getByRole("button", { name: "查看引擎" });
    fireEvent.click(trigger);
    expect(await screen.findByText(asset.name)).toBeVisible();
    expect(onUpdated).toHaveBeenCalledWith(asset);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("probes once, prevents duplicate requests and reads the updated status", async () => {
    let finish!: () => void;
    const probeEngine = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    const latest = { ...asset, status: "unhealthy" as const, status_basis: "连接拒绝" };
    const getEngine = vi.fn().mockResolvedValueOnce(asset).mockResolvedValue(latest);
    const { onUpdated } = setup(getEngine, probeEngine);
    fireEvent.click(screen.getByRole("button", { name: "查看引擎" }));
    await screen.findByText(asset.name);
    fireEvent.click(screen.getByRole("button", { name: "复检" }));
    await waitFor(() => expect(probeEngine).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: "正在复检…" })).toBeDisabled();
    finish();
    expect(await screen.findByText("连接拒绝")).toBeVisible();
    expect(getEngine).toHaveBeenCalledTimes(2);
    expect(onUpdated).toHaveBeenLastCalledWith(latest);
  });

  it("supports retrying a read error", async () => {
    const getEngine = vi.fn().mockRejectedValueOnce(new ApiError(503, "unavailable", "服务暂不可用")).mockResolvedValue(asset);
    setup(getEngine);
    fireEvent.click(screen.getByRole("button", { name: "查看引擎" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("引擎信息读取失败");
    fireEvent.click(screen.getByRole("button", { name: "重新读取" }));
    expect(await screen.findByText(asset.name)).toBeVisible();
  });

  it("keeps details after a failed probe and labels failed refreshes as stale", async () => {
    const getEngine = vi.fn().mockResolvedValueOnce(asset).mockRejectedValue(new ApiError(503, "unavailable", "服务暂不可用"));
    const probeEngine = vi.fn().mockRejectedValue(new Error("探测超时"));
    setup(getEngine, probeEngine);
    fireEvent.click(screen.getByRole("button", { name: "查看引擎" }));
    await screen.findByText(asset.name);
    fireEvent.click(screen.getByRole("button", { name: "复检" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("引擎复检失败");
    expect(screen.getByText(asset.name)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重新读取" }));
    expect(await screen.findByText("以下为上次读取的信息。")).toBeVisible();
    expect(screen.getByText(asset.name)).toBeVisible();
  });

  it("can inspect a disabled engine but cannot probe it", async () => {
    setup(vi.fn().mockResolvedValue({ ...asset, enabled: false, status: "disabled" }));
    fireEvent.click(screen.getByRole("button", { name: "查看引擎" }));
    await screen.findByText(asset.name);
    expect(screen.getByRole("button", { name: "复检" })).toBeDisabled();
    expect(screen.getByText(/引擎已停用，无法复检/)).toBeVisible();
  });

  it("shows removed engines and allows opening the management page", async () => {
    setup(vi.fn().mockRejectedValue(new ApiError(404, "not_found", "未找到引擎")));
    fireEvent.click(screen.getByRole("button", { name: "查看引擎" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("引擎不存在或已被删除");
    expect(screen.getByRole("button", { name: "复检" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "进入引擎管理" })).toHaveAttribute("href", "/engines");
  });

  it("aborts an unfinished read when closed", async () => {
    let signal!: AbortSignal;
    const getEngine = vi.fn((_id: string, input: AbortSignal) => { signal = input; return new Promise<EngineAsset>(() => {}); });
    const { onUpdated } = setup(getEngine);
    fireEvent.click(screen.getByRole("button", { name: "查看引擎" }));
    await screen.findByRole("status");
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "关闭引擎详情" }));
    await waitFor(() => expect(signal.aborted).toBe(true));
    expect(onUpdated).not.toHaveBeenCalled();
  });

  it("explains unbound engines without a request", () => {
    const { getEngine } = setup(undefined, undefined, null);
    expect(screen.getByText("绑定引擎后可查看状态与复检。")).toBeVisible();
    expect(screen.queryByRole("button", { name: "查看引擎" })).not.toBeInTheDocument();
    expect(getEngine).not.toHaveBeenCalled();
  });
});
