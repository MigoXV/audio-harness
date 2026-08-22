import { describe, expect, it } from "vitest";

import type { AudioWorkflow, VoiceAgentDefinition } from "../../api/types";
import { buildVoiceAgentGraph } from "./voiceAgentGraphModel";

const definition: VoiceAgentDefinition = {
  schema_version: 1,
  audio_workflow_id: "workflow-1",
  responses_engine_id: null,
  responses: {
    model: "",
    thinking_enabled: false,
    instructions: "",
    max_output_tokens: 1024,
    max_tool_rounds: 8,
    max_parallel_tools: 4,
    tool_timeout_seconds: 30,
    service_tools: [],
    mcp_servers: [],
    history_max_items: 128,
  },
  tts_engine_id: null,
  tts: { default_voice: "", allowed_voices: [], language: "Auto", decoder_chunk_size: 0 },
};

const workflows = [{ id: "workflow-1", name: "主语音图", active_revision: 3 }] as AudioWorkflow[];

describe("buildVoiceAgentGraph", () => {
  it("keeps the workflow graph focused on the runtime audio path", () => {
    const graph = buildVoiceAgentGraph({
      definition, workflows, engines: [], backends: [], diagnostics: [],
    });

    expect(graph.nodes.map((node) => node.id)).toEqual([
      "$input", "workflow", "responses", "backend", "tts", "$output",
    ]);
    expect(graph.nodes.find((node) => node.id === "workflow")?.data).toEqual(expect.objectContaining({
      label: "语音工作流",
      summary: "主语音图 · v3",
      state: "configured",
    }));
    expect(graph.nodes.some((node) => node.id.startsWith("workflow:"))).toBe(false);
    expect(graph.edges).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "input-workflow", source: "$input", target: "workflow" }),
      expect.objectContaining({ id: "workflow-responses", source: "workflow", target: "responses" }),
    ]));
  });
});
