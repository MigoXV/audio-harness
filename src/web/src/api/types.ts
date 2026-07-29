export type EngineKind = "asr" | "vad" | "asr_vad";
export type EngineApi = "ux_speech" | "ux_vad";
export type EngineProtocol = "grpc";
export type EngineRole = "asr" | "vad" | "semantic_vad";
export type EngineStatus =
  | "observed_running"
  | "confirmed_running"
  | "inactive";

export interface EngineAsset {
  id: string;
  slug: string;
  name: string;
  kind: EngineKind;
  api: EngineApi;
  protocol: EngineProtocol;
  target: string;
  connect_timeout_seconds: number;
  request_timeout_seconds: number | null;
  enabled: boolean;
  status: EngineStatus;
  image: string | null;
  description: string | null;
  status_basis: string | null;
  status_observed_at: string | null;
  source: string | null;
  details: Record<string, unknown>;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface EngineListResponse {
  items: EngineAsset[];
  total: number;
  limit: number;
  offset: number;
}

export interface EngineWritePayload {
  slug: string;
  name: string;
  kind: EngineKind;
  api: EngineApi;
  protocol: EngineProtocol;
  target: string;
  connect_timeout_seconds: number;
  request_timeout_seconds: number | null;
  enabled: boolean;
  status: EngineStatus;
  image: string | null;
  description: string | null;
  status_basis: string | null;
  status_observed_at: string | null;
  source: string | null;
  details: Record<string, unknown>;
}

export interface EngineBinding {
  role: EngineRole;
  engine: EngineAsset | null;
  generation: number;
  updated_at: string | null;
}

export interface HealthStatus {
  status: string;
  database: string;
  data_plane: string;
  problems: string[];
}

export interface EngineFilters {
  limit?: number;
  offset?: number;
  kind?: EngineKind;
  api?: EngineApi;
  enabled?: boolean;
  q?: string;
}

export interface AudioHarnessApi {
  listEngines(
    filters?: EngineFilters,
    signal?: AbortSignal,
  ): Promise<EngineListResponse>;
  createEngine(payload: EngineWritePayload): Promise<EngineAsset>;
  updateEngine(
    engineId: string,
    payload: Partial<EngineWritePayload>,
  ): Promise<EngineAsset>;
  deleteEngine(engineId: string): Promise<void>;
  listBindings(signal?: AbortSignal): Promise<EngineBinding[]>;
  bind(role: EngineRole, engineId: string): Promise<EngineBinding>;
  unbind(role: EngineRole): Promise<void>;
  getReadiness(signal?: AbortSignal): Promise<HealthStatus>;
}
