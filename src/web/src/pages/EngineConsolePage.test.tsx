import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { AudioHarnessApi, EngineAsset, EngineKind, EngineWritePayload } from "../api/types";
import { EngineConsolePage } from "./EngineConsolePage";
import { kindMeta, kinds } from "../features/engines/model";

// Layout and safe-area CSS are verified in Chromium; jsdom cannot resolve dvh/env math.
vi.mock("../features/engines/console.css", () => ({}));

const asset: EngineAsset = {
  id: "asr-1", slug: "asr-main", name: "主 ASR", kind: "asr", api: "ux_speech", protocol: "grpc", target: "localhost:50051",
  connect_timeout_seconds: 10, request_timeout_seconds: null, credential_env: null, api_key: null, enabled: true, status: "healthy", image: null,
  description: null, status_basis: "人工连通性校验通过", status_observed_at: "2026-10-01T00:00:00Z", last_success_at: null, probe_latency_ms: 12,
  consecutive_failures: 0, next_probe_at: null, source: null, details: { opaque_extension: { preserved: true } }, revision: 1,
  created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z",
};
function fixture(initial: EngineAsset[] = [asset]) {
  let items = initial.map(item => structuredClone(item));
  const api = {
    listEngines: vi.fn(async (filters = { limit: 500, offset: 0 }) => ({ items: items.slice(filters.offset ?? 0, (filters.offset ?? 0) + (filters.limit ?? 500)), total: items.length, limit: filters.limit, offset: filters.offset })),
    getEngine: vi.fn(async (id: string) => { const item = items.find(value => value.id === id); if (!item) throw new ApiError(404, "missing", "不存在"); return structuredClone(item); }),
    createEngine: vi.fn(async (payload: EngineWritePayload) => { const item = { ...asset, ...payload, id: `new-${items.length}`, status: "unknown" as const }; items.push(item); return item; }),
    updateEngine: vi.fn(async (id: string, payload: Partial<EngineWritePayload>) => { items = items.map(item => item.id === id ? { ...item, ...payload } : item); return items.find(item => item.id === id)!; }),
    deleteEngine: vi.fn(async (id: string) => { items = items.filter(item => item.id !== id); }),
    probeEngine: vi.fn(async (id: string) => ({ engine_id: id, name: asset.name, revision: 1, outcome: "passed", message: null as string | null, latency_ms: 12 })),
    validateEngines: vi.fn(async () => ({ results: items.filter(item => item.enabled).map(item => ({ engine_id: item.id, name: item.name, revision: 1, outcome: "passed" })) })),
    getEngineUsage: vi.fn(async (id: string) => ({ engine_id: id, references: [] as string[] })),
    listEngineModels: vi.fn(async () => ({ engine_id: asset.id, engine_revision: 1, items: ["model-a", "model-b"] })),
    listEnginePresetVoices: vi.fn(async () => ({ engine_id: asset.id, engine_revision: 1, serving_mode: "preset", items: [{ voice_id: "cherry", name: "Cherry", language: "zh", supports_synthesize: true, supports_duplex: false, available_on_instance: true }] })),
  };
  return { api, typed: api as unknown as AudioHarnessApi };
}
function setup(api: AudioHarnessApi) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const router = createMemoryRouter([{ path: "/", element: <EngineConsolePage api={api} refreshVersion={0} /> }, { path: "/else", element: <h1>其他页面</h1> }]);
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return { router, client };
}
async function edit(name = asset.name) { fireEvent.click(await screen.findByRole("button", { name: `编辑 ${name}` })); return screen.getByRole("dialog", { name: `编辑 ${name}` }); }
async function details(name = asset.name) {
  fireEvent.click(await screen.findByRole("button", { name }));
  const dialog = screen.getByRole("dialog", { name: "引擎详情" });
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "编辑配置" })).toBeEnabled());
  return dialog;
}
function fill(dialog: HTMLElement, label: RegExp, value: string) { fireEvent.change(within(dialog).getByLabelText(label), { target: { value } }); }

describe("engine management and diagnostics", () => {
  it("combines compact health, capability and search filters while keeping row actions available", async () => {
    const items = Array.from({ length: 19 }, (_, i) => ({ ...asset, id: `engine-${i}`, name: `引擎 ${i}`, slug: `engine-${i}`, kind: i === 13 ? "tts" as const : "asr" as const, enabled: i < 16, status: i < 12 ? "healthy" as const : i < 16 ? "unhealthy" as const : "disabled" as const }));
    const { typed } = fixture(items); setup(typed);
    await screen.findByText("19 个引擎");
    expect(screen.getByRole("button", { name: /^健康/ })).toHaveTextContent("健康12");
    expect(screen.getByRole("button", { name: /^异常/ })).toHaveTextContent("异常4");
    fireEvent.click(screen.getByRole("button", { name: /^异常/ }));
    expect(screen.getByText("4 个引擎")).toBeVisible();
    fireEvent.change(screen.getByRole("combobox", { name: "能力类型" }), { target: { value: "asr" } });
    fireEvent.change(screen.getByLabelText("搜索引擎"), { target: { value: "engine-12" } });
    expect(screen.getByText("1 个引擎")).toBeVisible();
    expect(screen.getByRole("button", { name: "编辑 引擎 12" })).toBeVisible();
    expect(screen.getByRole("button", { name: "复检 引擎 12" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("搜索引擎"), { target: { value: "missing" } });
    expect(screen.getByText("0 个引擎")).toBeVisible();
    expect(screen.getByText("没有符合条件的引擎")).toBeVisible();
    fireEvent.click(screen.getAllByRole("button", { name: "清空筛选" })[0]);
    expect(screen.getByText("19 个引擎")).toBeVisible();
    expect(screen.getByRole("button", { name: /^全部/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "复检 引擎 18" })).toBeDisabled();
  });
  it("edits directly and submits only the changed name, preserving runtime state and extensions", async () => {
    const { api, typed } = fixture(); setup(typed);
    const dialog = await edit(); fill(dialog, /^名称/, "新的名称");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存引擎" }));
    await waitFor(() => expect(api.updateEngine).toHaveBeenCalledWith(asset.id, { name: "新的名称" }));
    expect(await screen.findByRole("button", { name: "新的名称" })).toBeVisible();
    expect(api.getEngineUsage).not.toHaveBeenCalled();
  });
  it.each(kinds)("registers %s with its existing API and protocol", async (kind: EngineKind) => {
    const { api, typed } = fixture(); setup(typed);
    await screen.findByRole("button", { name: asset.name });
    fireEvent.click(screen.getByRole("button", { name: "注册引擎" }));
    const dialog = screen.getByRole("dialog", { name: "注册引擎" });
    fireEvent.change(within(dialog).getByLabelText("能力类型"), { target: { value: kind } });
    fill(dialog, /^名称/, "Primary Engine");
    fill(dialog, /^(gRPC 地址|API Base URL)/, kind === "llm" ? "https://example.com/v1" : "localhost:50051");
    if (kind === "llm") fill(dialog, /^服务进程环境变量名/, "MODEL_API_KEY");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存引擎" }));
    await waitFor(() => expect(api.createEngine).toHaveBeenCalledOnce());
    expect(api.createEngine.mock.calls[0][0]).toMatchObject({ name: "Primary Engine", slug: "primary-engine", kind, api: kindMeta[kind].api, protocol: kindMeta[kind].protocol });
    expect(api.createEngine.mock.calls[0][0]).not.toHaveProperty("label");
  });
  it("shows required and numeric errors beside fields before sending a request", async () => {
    const { api, typed } = fixture(); setup(typed); const dialog = await edit();
    fill(dialog, /^名称/, "   "); fill(dialog, /^Slug/, "bad--slug");
    fireEvent.click(within(dialog).getByText("高级参数")); fill(dialog, /^连接超时/, "0");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存引擎" }));
    expect(api.updateEngine).not.toHaveBeenCalled();
    expect(within(dialog).getByText("请填写有效的名称。")).toBeVisible();
    expect(within(dialog).getByText("请输入大于 0 的秒数。")).toBeVisible();
    await waitFor(() => expect(within(dialog).getByLabelText(/^名称/)).toHaveFocus());
  });
  it("keeps failed saves in the editor and maps 422 errors to fields", async () => {
    const { api, typed } = fixture(); api.updateEngine.mockRejectedValue(new ApiError(422, "request_validation_failed", "请求参数不合法", [{ loc: ["body", "name"], msg: "名称有误" }]));
    setup(typed); const dialog = await edit(); fill(dialog, /^名称/, "保留输入");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存引擎" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("请求参数不合法");
    expect(within(dialog).getByText("名称有误")).toBeVisible();
    expect(within(dialog).getByLabelText(/^名称/)).toHaveValue("保留输入");
  });
  it("protects dirty editor closes and route exits, with explicit discard", async () => {
    const { typed } = fixture(); const { router } = setup(typed); const dialog = await edit(); fill(dialog, /^名称/, "草稿");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(screen.getByRole("alertdialog", { name: "还有未保存的修改" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(within(dialog).getByLabelText(/^名称/)).toHaveValue("草稿");
    void router.navigate("/else");
    expect(await screen.findByRole("alertdialog", { name: "还有未保存的修改" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "放弃并离开" }));
    expect(await screen.findByRole("heading", { name: "其他页面" })).toBeVisible();
  });
  it("keeps a draft across a catalog refresh", async () => {
    const { typed } = fixture(); const { client } = setup(typed); const dialog = await edit(); fill(dialog, /^名称/, "未保存");
    await client.invalidateQueries({ queryKey: ["engine-console-list"] });
    expect(within(dialog).getByLabelText(/^名称/)).toHaveValue("未保存");
  });
  it("saves before probing and keeps the saved configuration after a failed probe", async () => {
    const { api, typed } = fixture(); api.probeEngine.mockRejectedValue(new Error("连接超时")); setup(typed);
    const dialog = await edit(); fill(dialog, /^名称/, "已保存名称");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存并复检" }));
    const detail = await screen.findByRole("dialog", { name: "引擎详情" });
    await waitFor(() => expect(within(detail).getByText("连接超时")).toBeVisible());
    expect(api.updateEngine).toHaveBeenCalledWith(asset.id, { name: "已保存名称" });
    expect(api.probeEngine).toHaveBeenCalledOnce();
    expect(within(detail).getByText(/已保存名称/)).toBeVisible();
  });
  it.each(["passed", "failed", "stale"])("interprets a %s probe and reloads the latest asset", async outcome => {
    const { api, typed } = fixture(); api.probeEngine.mockResolvedValue({ engine_id: asset.id, name: asset.name, revision: 1, outcome, message: "诊断结果", latency_ms: 12 }); setup(typed);
    fireEvent.click(await screen.findByRole("button", { name: `复检 ${asset.name}` }));
    expect(await screen.findByText(outcome === "passed" ? "复检通过：诊断结果" : outcome === "failed" ? "复检未通过：诊断结果" : "配置已变更，探测结果未写入：诊断结果")).toBeVisible();
    await waitFor(() => expect(api.getEngine).toHaveBeenCalledWith(asset.id));
  });
  it("locks only the probed engine and prevents repeated submissions", async () => {
    const second = { ...asset, id: "other", name: "其他引擎", slug: "other" };
    const { api, typed } = fixture([asset, second]); let finish!: (value: Awaited<ReturnType<typeof api.probeEngine>>) => void;
    api.probeEngine.mockImplementation(() => new Promise(resolve => { finish = resolve; })); setup(typed);
    const button = await screen.findByRole("button", { name: `复检 ${asset.name}` }); fireEvent.click(button); fireEvent.click(button);
    await waitFor(() => expect(api.probeEngine).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: `编辑 ${asset.name}` })).toBeDisabled();
    expect(screen.getByRole("button", { name: "编辑 其他引擎" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "复检全部已启用引擎" })).toBeDisabled();
    finish({ engine_id: asset.id, name: asset.name, revision: 1, outcome: "passed", message: null as string | null, latency_ms: 12 });
    await waitFor(() => expect(button).toBeEnabled());
  });
  it("labels old data when the post-probe reload fails", async () => {
    const { api, typed } = fixture(); api.getEngine.mockRejectedValue(new Error("读取超时")); setup(typed);
    fireEvent.click(await screen.findByRole("button", { name: `复检 ${asset.name}` }));
    expect(await screen.findByText(/最新信息读取失败，以下为旧信息/)).toBeVisible();
    expect(screen.getByRole("button", { name: asset.name })).toBeVisible();
  });
  it("shows all validation outcomes and prevents writes during full validation", async () => {
    const { api, typed } = fixture(); let finish!: (value: Awaited<ReturnType<typeof api.validateEngines>>) => void;
    api.validateEngines.mockImplementation(() => new Promise(resolve => { finish = resolve; })); setup(typed);
    await screen.findByRole("button", { name: asset.name });
    fireEvent.click(screen.getByRole("button", { name: "复检全部已启用引擎" }));
    await waitFor(() => expect(api.validateEngines).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: `编辑 ${asset.name}` })).toBeDisabled();
    finish({ results: ["passed", "failed", "stale"].map((outcome, index) => ({ engine_id: String(index), name: String(index), revision: 1, outcome })) });
    expect(await screen.findByText("全部复检完成：通过 1，失败 1，配置变更 1。")).toBeVisible();
    await waitFor(() => expect(api.listEngines).toHaveBeenCalledTimes(2));
  });
  it("reads beyond 200 and 500 assets, paginates 50 rows and searches the full catalog", async () => {
    const many = Array.from({ length: 501 }, (_, i) => ({ ...asset, id: `id-${i}`, slug: `engine-${i}`, name: `引擎 ${i}` }));
    const { api, typed } = fixture(many); setup(typed);
    await screen.findByText("501 个引擎 · 第 1 / 11 页");
    expect(api.listEngines).toHaveBeenNthCalledWith(2, { limit: 500, offset: 500 }, expect.any(AbortSignal));
    expect(screen.getAllByRole("button", { name: /^编辑 引擎/ })).toHaveLength(50);
    fireEvent.click(screen.getByRole("button", { name: "下一页" })); expect(screen.getByText("501 个引擎 · 第 2 / 11 页")).toBeVisible();
    fireEvent.change(screen.getByLabelText("搜索引擎"), { target: { value: "  engine-500  " } });
    expect(screen.getByRole("button", { name: "引擎 500" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "引擎 0" })).not.toBeInTheDocument();
  });
  it("does not publish partial catalog statistics when a later page fails", async () => {
    const { api, typed } = fixture(); api.listEngines.mockResolvedValueOnce({ items: [asset], total: 2, limit: 500, offset: 0 }).mockRejectedValue(new Error("第二页读取失败")); setup(typed);
    expect(await screen.findByRole("alert")).toHaveTextContent("第二页读取失败");
    expect(screen.queryByRole("button", { name: asset.name })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^健康/ })).toHaveTextContent("健康—");
  });
  it("gives disabled status priority and disables its probe", async () => {
    const { typed } = fixture([{ ...asset, enabled: false, status: "healthy" }]); setup(typed);
    const row = (await screen.findByRole("button", { name: asset.name })).closest("tr")!;
    expect(within(row).getByText("已停用")).toBeVisible();
    expect(within(row).getByRole("button", { name: `复检 ${asset.name}` })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^健康/ })).toHaveTextContent("健康0");
  });
  it("distinguishes failed references from no references and retries", async () => {
    const { api, typed } = fixture(); api.getEngineUsage.mockRejectedValueOnce(new Error("引用读取失败")); setup(typed); const dialog = await details();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("引用读取失败");
    expect(within(dialog).queryByText("当前没有工作流或智能体引用该引擎。")).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "重试引用读取" }));
    expect(await within(dialog).findByText("当前没有工作流或智能体引用该引擎。")).toBeVisible();
  });
  it.each(["disable", "delete"])("blocks %s when references exist", async action => {
    const { api, typed } = fixture(); api.getEngineUsage.mockResolvedValue({ engine_id: asset.id, references: ["主语音图 草稿"] }); setup(typed); const dialog = await details();
    fireEvent.click(within(dialog).getByRole("button", { name: action === "disable" ? "停用" : "删除引擎" }));
    const confirm = await screen.findByRole("alertdialog", { name: action === "disable" ? "当前不能停用引擎" : "当前不能删除引擎" });
    expect(within(confirm).getByText("主语音图 草稿")).toBeVisible();
    expect(within(confirm).queryByRole("button", { name: /^确认/ })).not.toBeInTheDocument();
    expect(api.deleteEngine).not.toHaveBeenCalled(); expect(api.updateEngine).not.toHaveBeenCalled();
  });
  it("keeps a rejected delete open for retry", async () => {
    const { api, typed } = fixture(); api.deleteEngine.mockRejectedValueOnce(new ApiError(409, "conflict", "新增引用阻止删除")); setup(typed); const dialog = await details();
    fireEvent.click(within(dialog).getByRole("button", { name: "删除引擎" })); const confirm = screen.getByRole("alertdialog");
    await waitFor(() => expect(within(confirm).getByRole("button", { name: "确认删除" })).toBeEnabled()); fireEvent.click(within(confirm).getByRole("button", { name: "确认删除" }));
    expect(await within(confirm).findByRole("alert")).toHaveTextContent("新增引用阻止删除");
    fireEvent.click(within(confirm).getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(await screen.findByText("尚未注册引擎")).toBeVisible();
  });
  it("lazily reads and searches Responses models", async () => {
    const llm = { ...asset, kind: "llm" as const, api: "responses" as const, protocol: "https" as const, target: "https://example.com/v1", credential_env: "KEY" };
    const { api, typed } = fixture([llm]); setup(typed); const dialog = await details(); expect(api.listEngineModels).not.toHaveBeenCalled();
    fireEvent.mouseDown(within(dialog).getByRole("tab", { name: "模型目录" }), { button: 0, ctrlKey: false }); expect(await within(dialog).findByText("model-a")).toBeVisible();
    fireEvent.change(within(dialog).getByLabelText("搜索模型"), { target: { value: "model-b" } });
    expect(within(dialog).queryByText("model-a")).not.toBeInTheDocument(); expect(within(dialog).getByText("model-b")).toBeVisible();
  });
  it("shows TTS voice capabilities and instance availability", async () => {
    const { api, typed } = fixture([{ ...asset, kind: "tts", api: "qwen_tts_duplex" }]); setup(typed); const dialog = await details(); expect(api.listEnginePresetVoices).not.toHaveBeenCalled();
    fireEvent.mouseDown(within(dialog).getByRole("tab", { name: "音色目录" }), { button: 0, ctrlKey: false }); expect(await within(dialog).findByText("Cherry")).toBeVisible();
    expect(within(dialog).getByText("zh · 支持合成 · 不支持双工")).toBeVisible(); expect(within(dialog).getByText("本实例可用")).toBeVisible();
  });
  it("does not read discovery catalogs for disabled engines", async () => {
    const { api, typed } = fixture([{ ...asset, kind: "llm", api: "responses", enabled: false }]); setup(typed); const dialog = await details();
    fireEvent.mouseDown(within(dialog).getByRole("tab", { name: "模型目录" }), { button: 0, ctrlKey: false }); expect(within(dialog).getByText("引擎已停用，启用后可读取模型目录。")).toBeVisible();
    expect(api.listEngineModels).not.toHaveBeenCalled();
  });
  it("restores the row trigger after closing details", async () => {
    const { typed } = fixture(); setup(typed); const trigger = await screen.findByRole("button", { name: asset.name }); trigger.focus();
    const dialog = await details(); fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(trigger).toHaveFocus());
  });
  it("preserves focus moved to the next action before close restoration runs", async () => {
    const { typed } = fixture(); setup(typed);
    const dialog = await details();
    fireEvent.keyDown(dialog, { key: "Escape" });
    const next = screen.getByRole("button", { name: `编辑 ${asset.name}` }); next.focus();
    await new Promise(resolve => requestAnimationFrame(resolve));
    expect(next).toHaveFocus();
  });
  it("registers the duplex diarization implementation", async () => {
    const { api, typed } = fixture(); setup(typed); await screen.findByRole("button", { name: asset.name });
    fireEvent.click(screen.getByRole("button", { name: "注册引擎" })); const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("能力类型"), { target: { value: "diarization" } });
    fireEvent.change(within(dialog).getByLabelText("实现"), { target: { value: "duplex" } });
    fill(dialog, /^名称/, "Sortformer"); fill(dialog, /^gRPC 地址/, "localhost:50051");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存引擎" }));
    await waitFor(() => expect(api.createEngine).toHaveBeenCalledWith(expect.objectContaining({ api: "ux_speaker_diarization_duplex", protocol: "grpc", target: "localhost:50051" })));
  });
  it("registers the built-in diarization implementation with its fixed address", async () => {
    const { api, typed } = fixture(); setup(typed); await screen.findByRole("button", { name: asset.name });
    fireEvent.click(screen.getByRole("button", { name: "注册引擎" })); const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("能力类型"), { target: { value: "diarization" } });
    fireEvent.change(within(dialog).getByLabelText("实现"), { target: { value: "dummy" } });
    fill(dialog, /^名称/, "Built In"); fireEvent.click(within(dialog).getByRole("button", { name: "保存引擎" }));
    await waitFor(() => expect(api.createEngine).toHaveBeenCalledWith(expect.objectContaining({ api: "dummy_diarize", protocol: "dummy", target: "dummy://diarize" })));
  });
  it("registers the built-in clustering implementation with its fixed address", async () => {
    const { api, typed } = fixture(); setup(typed); await screen.findByRole("button", { name: asset.name });
    fireEvent.click(screen.getByRole("button", { name: "注册引擎" })); const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("能力类型"), { target: { value: "embedding" } });
    fireEvent.change(within(dialog).getByLabelText(/^实现/), { target: { value: "dummy" } });
    expect(within(dialog).getByLabelText("内置地址")).toHaveAttribute("readonly");
    fill(dialog, /^名称/, "Dummy Cluster"); fireEvent.click(within(dialog).getByRole("button", { name: "保存引擎" }));
    await waitFor(() => expect(api.createEngine).toHaveBeenCalledWith(expect.objectContaining({ kind: "embedding", api: "dummy_clustering", protocol: "dummy", target: "dummy://clustering" })));
  });
  it("validates VAD overrides and preserves unrelated extension data", async () => {
    const { api, typed } = fixture([{ ...asset, kind: "vad", api: "ux_vad" }]); setup(typed); const dialog = await edit();
    fireEvent.click(within(dialog).getByText("高级参数")); fireEvent.click(within(dialog).getByLabelText("覆盖 VAD 服务端默认后处理"));
    fill(dialog, /^语音阈值/, "2"); fireEvent.click(within(dialog).getByRole("button", { name: "保存引擎" }));
    expect(within(dialog).getByText("请输入 0 到 1 之间的阈值。")).toBeVisible(); expect(api.updateEngine).not.toHaveBeenCalled();
    fill(dialog, /^语音阈值/, ".6"); fireEvent.click(within(dialog).getByRole("button", { name: "保存引擎" }));
    await waitFor(() => expect(api.updateEngine).toHaveBeenCalledWith(asset.id, { details: expect.objectContaining({ opaque_extension: { preserved: true }, vad_postprocessing: expect.objectContaining({ parameters: expect.objectContaining({ speech_threshold: .6 }) }) }) }));
  });
  it("prevents duplicate saves while preserving a failed editor for retry", async () => {
    const { api, typed } = fixture(); let fail!: (reason: Error) => void;
    api.updateEngine.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; })); setup(typed); const dialog = await edit(); fill(dialog, /^名称/, "保存一次");
    const button = within(dialog).getByRole("button", { name: "保存引擎" }); fireEvent.click(button); fireEvent.click(button);
    await waitFor(() => expect(api.updateEngine).toHaveBeenCalledOnce());
    expect(within(dialog).getByRole("button", { name: "关闭编辑引擎" })).toBeDisabled();
    fail(new Error("临时失败")); expect(await within(dialog).findByRole("alert")).toHaveTextContent("临时失败");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存引擎" }));
    expect(await screen.findByRole("button", { name: "保存一次" })).toBeVisible();
  });
  it("keeps the old catalog visible and hides counts when refresh fails", async () => {
    const { api, typed } = fixture(); setup(typed); await screen.findByRole("button", { name: asset.name });
    api.listEngines.mockRejectedValueOnce(new Error("刷新失败")); fireEvent.click(screen.getByRole("button", { name: "刷新列表" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("统计暂不可用");
    expect(screen.getByRole("button", { name: asset.name })).toBeVisible(); expect(screen.getByRole("button", { name: /^健康/ })).toHaveTextContent("健康—");
  });
  it("retries a model directory failure without pretending it is empty", async () => {
    const { api, typed } = fixture([{ ...asset, kind: "llm", api: "responses", protocol: "https", target: "https://example.com/v1", api_key: "key" }]);
    api.listEngineModels.mockRejectedValueOnce(new Error("模型目录超时")); setup(typed); const dialog = await details();
    fireEvent.mouseDown(within(dialog).getByRole("tab", { name: "模型目录" }), { button: 0, ctrlKey: false });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("模型目录超时");
    expect(within(dialog).queryByText("引擎未报告可用模型。")).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "刷新模型目录" })); expect(await within(dialog).findByText("model-a")).toBeVisible();
  });
});
