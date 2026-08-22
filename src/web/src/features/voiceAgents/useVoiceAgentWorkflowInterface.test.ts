import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AudioHarnessApi, AudioWorkflowRevision } from "../../api/types";
import { useVoiceAgentWorkflowInterface } from "./useVoiceAgentWorkflowInterface";

function revision(outputs: string[]): AudioWorkflowRevision {
  return {
    workflow_id: "workflow-1",
    revision: 1,
    document: {
      schema_version: 1,
      nodes: [],
      edges: [],
      outputs: outputs.map((name) => ({ name, source: { kind: "node", node_id: "source", port: "output" } })),
      layout: {},
    },
    engine_revisions: {},
    activated_at: "2026-08-21T00:00:00Z",
  };
}

describe("useVoiceAgentWorkflowInterface", () => {
  it("detects the speaker_filter interface and clears stale workflow state", async () => {
    const getActiveAudioWorkflow = vi.fn()
      .mockResolvedValueOnce(revision(["primary_transcript", "speaker_filter"]))
      .mockResolvedValueOnce(revision(["primary_transcript"]));
    const api = { getActiveAudioWorkflow } as unknown as AudioHarnessApi;
    const { result, rerender } = renderHook(
      ({ workflowId }) => useVoiceAgentWorkflowInterface(api, workflowId),
      { initialProps: { workflowId: "workflow-1" } },
    );

    expect(result.current.status).toBe("loading");
    await waitFor(() => expect(result.current.status).toBe("available"));

    rerender({ workflowId: "workflow-2" });
    expect(result.current.status).toBe("loading");
    await waitFor(() => expect(result.current.status).toBe("unavailable"));
  });

  it("reports interface read failures separately from an unavailable interface", async () => {
    const api = {
      getActiveAudioWorkflow: vi.fn().mockRejectedValue(new Error("network down")),
    } as unknown as AudioHarnessApi;
    const { result } = renderHook(() => useVoiceAgentWorkflowInterface(api, "workflow-1"));

    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.error).toContain("network down");
  });
});
