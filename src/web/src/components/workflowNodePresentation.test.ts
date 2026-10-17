import { describe, expect, it } from "vitest";
import type { AudioNode, EngineAsset } from "../api/types";
import { workflowNodePresentation, workflowNodeSummary } from "./workflowNodePresentation";

describe("工作流节点的真实配置摘要", () => {
  it(" distinguishes a missing service from a disabled service", () => {
    const node: AudioNode = { id: "asr", kind: "asr", engine_id: "gone", config: { remove_punctuation: true } };
    expect(workflowNodeSummary(node, [])).toEqual([["服务", "服务不可用"], ["标点", "移除标点"]]);
    const engine = { id: "gone", name: "ASR 服务", enabled: false } as EngineAsset;
    expect(workflowNodeSummary(node, [engine])[0][1]).toBe("ASR 服务 · 已停用");
  });
  it("does not invent a model for infrastructure nodes", () => {
    const node: AudioNode = { id: "search", kind: "vector_search", config: { collection_name: "voices", vector_name: "embedding", include_embedding: false, top_k: 5, filter: {} } };
    expect(workflowNodeSummary(node, [])).toEqual([["集合", "voices"], ["向量", "embedding"]]);
  });
  it("keeps new node types visible through their actual display name", () => {
    expect(workflowNodePresentation("new_kind", "新增能力").title).toBe("新增能力");
  });
});
