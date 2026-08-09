import type { VoiceAgentDefinition } from "../../api/types";

export function normalizeSlug(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
}

export function defaultVoiceAgentDefinition(workflowId = ""): VoiceAgentDefinition {
  return {
    schema_version: 1,
    audio_workflow_id: workflowId,
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
    tts: {
      default_voice: "",
      allowed_voices: [],
      language: "Auto",
      decoder_chunk_size: 0,
    },
  };
}
