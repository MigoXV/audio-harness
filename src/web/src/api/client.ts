import type {
  AudioHarnessApi,
  CapacitySettings,
  CapacitySettingsUpdate,
  EngineAsset,
  EngineFilters,
  EngineListResponse,
  EngineValidation,
  HealthStatus,
  NodeTypeDefinition,
  PipelineDraft,
  PipelineRevision,
  PipelineValidation,
  WorkflowSettings,
  WorkflowSettingsUpdate,
  WorkflowCreatePayload,
  WorkflowSummary,
  ModelRoute,
  ManagedWorkflowCreatePayload,
  ManagedWorkflowDraft,
  StageWorkflowDefinition,
  WorkflowStatus,
  RuntimeStatus,
} from "./types";

interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(
    status: number,
    code: string,
    message: string,
    details: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function parseBody(response: Response): Promise<unknown> {
  if (response.status === 204) {
    return undefined;
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    return response.text();
  }
  return response.json();
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");

  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers,
  });
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

function engineQuery(filters: EngineFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") {
      params.set(key, String(value));
    }
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

function workflowPath(workflowId: string | undefined, suffix = ""): string {
  return workflowId
    ? `/api/v1/workflows/${encodeURIComponent(workflowId)}${suffix}`
    : `/api/v1/pipeline${suffix}`;
}

export const apiClient: AudioHarnessApi = {
  listEngines(filters = {}, signal) {
    return request<EngineListResponse>(
      `/api/v1/engines${engineQuery(filters)}`,
      { signal },
    );
  },

  getEngine(engineId, signal) {
    return request<EngineAsset>(`/api/v1/engines/${engineId}`, {
      signal,
    });
  },

  createEngine(payload) {
    return request<EngineAsset>("/api/v1/engines", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  updateEngine(engineId, payload) {
    return request<EngineAsset>(`/api/v1/engines/${engineId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  async deleteEngine(engineId) {
    await request<void>(`/api/v1/engines/${engineId}`, {
      method: "DELETE",
    });
  },

  validateEngines() {
    return request<EngineValidation>("/api/v1/engines/validate", {
      method: "POST",
    });
  },

  listNodeTypes(signal) {
    return request<NodeTypeDefinition[]>("/api/v1/node-types", { signal });
  },

  getPipeline(signal, workflowId) {
    return request<PipelineDraft>(
      workflowId
        ? workflowPath(workflowId, "/draft")
        : "/api/v1/pipeline",
      { signal },
    );
  },

  updatePipelineDraft(definition, expectedRevision, workflowId) {
    return request<PipelineDraft>(workflowPath(workflowId, "/draft"), {
      method: "PUT",
      body: JSON.stringify({
        definition,
        expected_revision: expectedRevision,
      }),
    });
  },

  validatePipeline(workflowId) {
    return request<PipelineValidation>(workflowPath(workflowId, "/validate"), {
      method: "POST",
    });
  },

  activatePipeline(expectedRevision, workflowId) {
    return request<PipelineRevision>(workflowPath(workflowId, "/activate"), {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision }),
    });
  },

  getActivePipeline(signal, workflowId) {
    return request<PipelineRevision>(workflowPath(workflowId, "/active"), {
      signal,
    });
  },

  listWorkflows(signal) {
    return request<WorkflowSummary[]>("/api/v1/workflows", { signal });
  },

  createWorkflow(payload: WorkflowCreatePayload) {
    return request<PipelineDraft>("/api/v1/workflows", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async deleteWorkflow(workflowId) {
    await request<void>(`/api/v1/workflows/${encodeURIComponent(workflowId)}`, {
      method: "DELETE",
    });
  },

  listModelRoutes(signal) {
    return request<ModelRoute[]>("/api/v1/model-routes", { signal });
  },

  putModelRoute(route) {
    return request<ModelRoute>(
      `/api/v1/model-routes/${encodeURIComponent(route.model)}`,
      { method: "PUT", body: JSON.stringify(route) },
    );
  },

  async deleteModelRoute(model) {
    await request<void>(`/api/v1/model-routes/${encodeURIComponent(model)}`, {
      method: "DELETE",
    });
  },

  async getReadiness(signal) {
    const response = await fetch("/health/ready", {
      signal,
      headers: { Accept: "application/json" },
    });
    const body = (await parseBody(response)) as HealthStatus | ErrorEnvelope;
    if (response.status === 200 || response.status === 503) {
      return body as HealthStatus;
    }
    const envelope = body as ErrorEnvelope;
    throw new ApiError(
      response.status,
      envelope.error?.code ?? "health_request_failed",
      envelope.error?.message ?? `健康状态读取失败（${response.status}）`,
      envelope.error?.details,
    );
  },

  getWorkflowSettings(signal) {
    return request<WorkflowSettings>("/api/v1/workflow-settings", { signal });
  },

  updateWorkflowSettings(payload: WorkflowSettingsUpdate) {
    return request<WorkflowSettings>("/api/v1/workflow-settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },

  getCapacitySettings(signal) {
    return request<CapacitySettings>("/api/v1/capacity-settings", {
      signal,
    });
  },

  updateCapacitySettings(payload: CapacitySettingsUpdate) {
    return request<CapacitySettings>("/api/v1/capacity-settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },

  createManagedWorkflow(payload: ManagedWorkflowCreatePayload) {
    return request<ManagedWorkflowDraft>("/api/v1/workflows", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  getManagedWorkflowDraft(workflowId, signal) {
    return request<ManagedWorkflowDraft>(
      `/api/v1/workflows/${encodeURIComponent(workflowId)}/draft`,
      { signal },
    );
  },

  saveManagedWorkflowDraft(workflowId, definition: StageWorkflowDefinition, expectedRevision) {
    return request<ManagedWorkflowDraft>(
      `/api/v1/workflows/${encodeURIComponent(workflowId)}/draft`,
      {
        method: "PUT",
        body: JSON.stringify({
          definition,
          expected_revision: expectedRevision,
        }),
      },
    );
  },

  publishWorkflow(workflowId, expectedRevision) {
    return request<PipelineRevision>(
      `/api/v1/workflows/${encodeURIComponent(workflowId)}/publish`,
      {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      },
    );
  },

  getWorkflowStatus(workflowId, signal) {
    return request<WorkflowStatus>(
      `/api/v1/workflows/${encodeURIComponent(workflowId)}/status`,
      { signal },
    );
  },

  listWorkflowRevisions(workflowId, signal) {
    return request<PipelineRevision[]>(
      `/api/v1/workflows/${encodeURIComponent(workflowId)}/revisions`,
      { signal },
    );
  },

  restoreWorkflowRevision(workflowId, revision, expectedRevision) {
    return request<ManagedWorkflowDraft>(
      `/api/v1/workflows/${encodeURIComponent(workflowId)}/revisions/${revision}/restore`,
      {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      },
    );
  },

  patchWorkflow(workflowId, payload) {
    return request<ManagedWorkflowDraft>(
      `/api/v1/workflows/${encodeURIComponent(workflowId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    );
  },

  probeEngine(engineId) {
    return request<EngineValidation["results"][number]>(
      `/api/v1/engines/${encodeURIComponent(engineId)}/probe`,
      { method: "POST" },
    );
  },

  getEngineUsage(engineId, signal) {
    return request<{ engine_id: string; references: string[] }>(
      `/api/v1/engines/${encodeURIComponent(engineId)}/usage`,
      { signal },
    );
  },

  getRuntimeStatus(signal) {
    return request<RuntimeStatus>("/api/v1/runtime/status", { signal });
  },
};

export function errorMessage(
  error: unknown,
  fallback = "操作未完成，请稍后重试。",
): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (
    error instanceof TypeError ||
    (error instanceof Error &&
      /failed to fetch|network|load failed/i.test(error.message))
  ) {
    return "暂时无法连接管理服务，请稍后重试。";
  }
  return fallback;
}

export function reportRequestError(context: string, error: unknown): void {
  console.error(`[Audio Harness] ${context}`, error);
}
