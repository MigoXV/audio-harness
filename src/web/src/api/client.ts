import type {
  AudioHarnessApi,
  AgentBackend,
  AgentBackendCapabilities,
  AgentBackendCreate,
  AgentBackendValidation,
  AudioNodeType,
  AudioWorkflow,
  AudioWorkflowDraft,
  AudioWorkflowRevision,
  AudioWorkflowValidation,
  CapacitySettings,
  EngineAsset,
  EngineFilters,
  EngineListResponse,
  EngineValidation,
  EngineValidationResult,
  RealtimeRoute,
  RuntimeOverview,
  RuntimeStatus,
  VoiceAgent,
  VoiceAgentDraft,
  VoiceAgentRevision,
  WorkflowSettings,
  VectorInfrastructure,
  VectorSpace,
  VectorSpeakerMatch,
  VectorPoint,
  VadAudioPreview,
} from "./types";

interface ErrorEnvelope {
  error?: { code?: string; message?: string; details?: unknown };
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function parseBody(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const contentType = response.headers.get("content-type") ?? "";
  return contentType.includes("application/json")
    ? response.json()
    : response.text();
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  headers.set("Accept", "application/json");
  const response = await fetch(path, { credentials: "same-origin", ...init, headers });
  const body = await parseBody(response);
  if (!response.ok) {
    const envelope = body as ErrorEnvelope;
    throw new ApiError(
      response.status,
      envelope.error?.code ?? "request_failed",
      envelope.error?.message ?? `请求失败（${response.status}）`,
      envelope.error?.details,
    );
  }
  return body as T;
}

function query(filters: EngineFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  return params.size ? `?${params}` : "";
}

const workflowPath = (id: string, suffix = "") =>
  `/api/v1/audio-workflows/${encodeURIComponent(id)}${suffix}`;

export const apiClient: AudioHarnessApi = {
  listEngines(filters = {}, signal) {
    return request<EngineListResponse>(`/api/v1/engines${query(filters)}`, { signal });
  },
  getEngine(id, signal) { return request<EngineAsset>(`/api/v1/engines/${encodeURIComponent(id)}`, { signal }); },
  createEngine(payload) { return request<EngineAsset>("/api/v1/engines", { method: "POST", body: JSON.stringify(payload) }); },
  updateEngine(id, payload) { return request<EngineAsset>(`/api/v1/engines/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(payload) }); },
  async deleteEngine(id) { await request(`/api/v1/engines/${encodeURIComponent(id)}`, { method: "DELETE" }); },
  validateEngines() { return request<EngineValidation>("/api/v1/engines/validate", { method: "POST" }); },
  async probeEngine(id) {
    const value = await request<EngineValidation>(`/api/v1/engines/${encodeURIComponent(id)}/validate`, { method: "POST" });
    return value.results[0] as EngineValidationResult;
  },
  getEngineUsage(id, signal) { return request(`/api/v1/engines/${encodeURIComponent(id)}/usage`, { signal }); },
  listEngineModels(id, signal) { return request(`/api/v1/engines/${encodeURIComponent(id)}/models`, { signal }); },
  listEnginePresetVoices(id, signal) { return request(`/api/v1/engines/${encodeURIComponent(id)}/voices`, { signal }); },

  listAudioNodeTypes(signal) { return request<AudioNodeType[]>("/api/v1/audio-node-types", { signal }); },
  listAudioWorkflows(signal) { return request<AudioWorkflow[]>("/api/v1/audio-workflows", { signal }); },
  createAudioWorkflow(payload) { return request<AudioWorkflow>("/api/v1/audio-workflows", { method: "POST", body: JSON.stringify(payload) }); },
  patchAudioWorkflow(id, payload) { return request<AudioWorkflow>(workflowPath(id), { method: "PATCH", body: JSON.stringify(payload) }); },
  async deleteAudioWorkflow(id) { await request(workflowPath(id), { method: "DELETE" }); },
  getAudioWorkflowDraft(id, signal) { return request<AudioWorkflowDraft>(workflowPath(id, "/draft"), { signal }); },
  saveAudioWorkflowDraft(id, document, expectedRevision) { return request<AudioWorkflowDraft>(workflowPath(id, "/draft"), { method: "PUT", body: JSON.stringify({ document, expected_revision: expectedRevision }) }); },
  validateAudioWorkflow(id) { return request<AudioWorkflowValidation>(workflowPath(id, "/validate"), { method: "POST" }); },
  publishAudioWorkflow(id, expectedRevision) { return request<AudioWorkflowRevision>(workflowPath(id, "/publish"), { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) }); },
  getActiveAudioWorkflow(id, signal) { return request<AudioWorkflowRevision>(workflowPath(id, "/active"), { signal }); },
  listAudioWorkflowRevisions(id, signal) { return request<AudioWorkflowRevision[]>(workflowPath(id, "/revisions"), { signal }); },
  restoreAudioWorkflowRevision(id, revision, expectedRevision) { return request<AudioWorkflowDraft>(workflowPath(id, `/revisions/${revision}/restore`), { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) }); },

  listVoiceAgents(signal) { return request<VoiceAgent[]>("/api/v1/voice-agents", { signal }); },
  createVoiceAgent(payload) { return request<VoiceAgent>("/api/v1/voice-agents", { method: "POST", body: JSON.stringify(payload) }); },
  getVoiceAgentDraft(id, signal) { return request<VoiceAgentDraft>(`/api/v1/voice-agents/${encodeURIComponent(id)}/draft`, { signal }); },
  saveVoiceAgentDraft(id, definition, expectedRevision) { return request<VoiceAgentDraft>(`/api/v1/voice-agents/${encodeURIComponent(id)}/draft`, { method: "PUT", body: JSON.stringify({ definition, expected_revision: expectedRevision }) }); },
  validateVoiceAgent(id) { return request(`/api/v1/voice-agents/${encodeURIComponent(id)}/validate`, { method: "POST" }); },
  publishVoiceAgent(id, expectedRevision) { return request<VoiceAgentRevision>(`/api/v1/voice-agents/${encodeURIComponent(id)}/publish`, { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) }); },
  listVoiceAgentRevisions(id, signal) { return request<VoiceAgentRevision[]>(`/api/v1/voice-agents/${encodeURIComponent(id)}/revisions`, { signal }); },
  restoreVoiceAgentRevision(id, revision, expectedRevision) { return request<VoiceAgentDraft>(`/api/v1/voice-agents/${encodeURIComponent(id)}/revisions/${revision}/restore`, { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) }); },
  listRealtimeRoutes(signal) { return request<RealtimeRoute[]>("/api/v1/realtime-routes", { signal }); },
  getRealtimeRoute(model, signal) { return request<RealtimeRoute>(`/api/v1/realtime-routes/${encodeURIComponent(model)}`, { signal }); },
  putRealtimeRoute(route) { return request<RealtimeRoute>(`/api/v1/realtime-routes/${encodeURIComponent(route.model)}`, { method: "PUT", body: JSON.stringify(route) }); },
  async deleteRealtimeRoute(model) { await request(`/api/v1/realtime-routes/${encodeURIComponent(model)}`, { method: "DELETE" }); },

  async listAgentBackends(signal) { return (await request<{ items: AgentBackend[] }>("/api/v1/agent-backends?limit=200", { signal })).items; },
  getAgentBackend(id, signal) { return request<AgentBackend>(`/api/v1/agent-backends/${encodeURIComponent(id)}`, { signal }); },
  createAgentBackend(payload: AgentBackendCreate) { return request<AgentBackend>("/api/v1/agent-backends", { method: "POST", body: JSON.stringify(payload) }); },
  updateAgentBackend(id, payload) { return request<AgentBackend>(`/api/v1/agent-backends/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(payload) }); },
  async deleteAgentBackend(id) { await request(`/api/v1/agent-backends/${encodeURIComponent(id)}`, { method: "DELETE" }); },
  validateAgentBackend(id) { return request<AgentBackendValidation>(`/api/v1/agent-backends/${encodeURIComponent(id)}/validate`, { method: "POST" }); },
  getAgentBackendCapabilities(id, signal) { return request<AgentBackendCapabilities>(`/api/v1/agent-backends/${encodeURIComponent(id)}/capabilities`, { signal }); },
  getAgentBackendUsage(id, signal) { return request(`/api/v1/agent-backends/${encodeURIComponent(id)}/usage`, { signal }); },

  getRuntimeOverview(signal) { return request<RuntimeOverview>("/api/v1/runtime/overview", { signal }); },
  getWorkflowSettings(signal) { return request<WorkflowSettings>("/api/v1/settings/workflow", { signal }); },
  updateWorkflowSettings(payload) {
    const body = { ...payload } as Record<string, unknown>;
    delete body.updated_at;
    return request<WorkflowSettings>("/api/v1/settings/workflow", { method: "PUT", body: JSON.stringify(body) });
  },
  getCapacitySettings(signal) { return request<CapacitySettings>("/api/v1/settings/capacity", { signal }); },
  updateCapacitySettings(payload) {
    const body = { ...payload } as Record<string, unknown>;
    delete body.updated_at;
    return request<CapacitySettings>("/api/v1/settings/capacity", { method: "PUT", body: JSON.stringify(body) });
  },
  getRuntimeStatus(signal) { return request<RuntimeStatus>("/api/v1/runtime/status", { signal }); },
  getVectorInfrastructure(signal) { return request<VectorInfrastructure>("/api/v1/vector-infrastructure", { signal }); },
  async listVectorSpaces(signal) { return (await request<{ items: VectorSpace[] }>("/api/v1/vector-spaces", { signal })).items; },
  createVectorSpace(payload) { return request<VectorSpace>("/api/v1/vector-spaces", { method: "POST", body: JSON.stringify(payload) }); },
  async destroyVectorSpace(collectionName, confirmation) { await request(`/api/v1/vector-spaces/${encodeURIComponent(collectionName)}/destroy`, { method: "POST", body: JSON.stringify({ confirmation }) }); },
  registerVectorAudio(id, form) { return request<{ point_id: string }>(`/api/v1/vector-spaces/${encodeURIComponent(id)}/points/audio`, { method: "POST", body: form }); },
  previewVectorSearchVad(id, form) { return request<VadAudioPreview>(`/api/v1/vector-spaces/${encodeURIComponent(id)}/search/audio/vad-preview`, { method: "POST", body: form }); },
  searchVectorAudio(id, form) { return request<{ embedding: number[]; groups: VectorSpeakerMatch[] }>(`/api/v1/vector-spaces/${encodeURIComponent(id)}/search/audio`, { method: "POST", body: form }); },
  scrollVectorPoints(id, payload) { return request<{ items: VectorPoint[]; next_offset: string | null }>(`/api/v1/vector-spaces/${encodeURIComponent(id)}/points/scroll`, { method: "POST", body: JSON.stringify(payload) }); },
  async replaceVectorPayload(id, pointId, payload) { await request(`/api/v1/vector-spaces/${encodeURIComponent(id)}/points/${encodeURIComponent(pointId)}/payload`, { method: "PUT", body: JSON.stringify({ payload }) }); },
  async deleteVectorPoint(id, pointId) { await request(`/api/v1/vector-spaces/${encodeURIComponent(id)}/points/${encodeURIComponent(pointId)}`, { method: "DELETE" }); },
};

export function errorMessage(error: unknown, fallback = "操作未完成，请稍后重试。"): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof TypeError && /fetch|network/i.test(error.message)) {
    return "暂时无法连接管理服务，请稍后重试。";
  }
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}
