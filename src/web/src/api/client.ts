import type {
  AudioHarnessApi,
  CapacitySettings,
  CapacitySettingsUpdate,
  EngineAsset,
  EngineFilters,
  EngineListResponse,
  HealthStatus,
  NodeTypeDefinition,
  PipelineDraft,
  PipelineRevision,
  PipelineValidation,
  WorkflowSettings,
  WorkflowSettingsUpdate,
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

  const response = await fetch(path, { ...init, headers });
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

  listNodeTypes(signal) {
    return request<NodeTypeDefinition[]>("/api/v1/node-types", { signal });
  },

  getPipeline(signal) {
    return request<PipelineDraft>("/api/v1/pipeline", { signal });
  },

  updatePipelineDraft(definition, expectedRevision) {
    return request<PipelineDraft>("/api/v1/pipeline/draft", {
      method: "PUT",
      body: JSON.stringify({
        definition,
        expected_revision: expectedRevision,
      }),
    });
  },

  validatePipeline() {
    return request<PipelineValidation>("/api/v1/pipeline/validate", {
      method: "POST",
    });
  },

  activatePipeline(expectedRevision) {
    return request<PipelineRevision>("/api/v1/pipeline/activate", {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision }),
    });
  },

  getActivePipeline(signal) {
    return request<PipelineRevision>("/api/v1/pipeline/active", {
      signal,
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
