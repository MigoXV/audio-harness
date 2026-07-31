export type SupportedEngineKind = "asr" | "vad";
export type EngineKind = SupportedEngineKind | (string & {});
export type EngineApi = "ux_speech" | "ux_vad" | (string & {});
export type EngineProtocol = "grpc" | (string & {});
export type KnownEngineRole = "asr" | "vad" | "semantic_vad";
export type EngineRole = KnownEngineRole | (string & {});
export type EngineStatus =
  | "observed_running"
  | "confirmed_running"
  | "inactive"
  | (string & {});

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

export interface WorkflowSettings {
  min_chunk_bytes: number;
  min_segment_seconds: number;
  max_segment_seconds: number;
  audio_buffer_seconds: number;
  speech_pre_roll_seconds: number;
  default_language_code: string;
  audio_queue_capacity: number;
  segment_queue_capacity: number;
  result_queue_capacity: number;
  revision: number;
  updated_at: string;
}

export type WorkflowSettingsUpdate = Omit<
  WorkflowSettings,
  "updated_at"
>;

export interface CapacitySettings {
  grpc: {
    max_sessions: number;
  };
  realtime: {
    max_sessions: number;
  };
  webrtc: {
    max_sessions: number;
  };
  max_asr_concurrency: number;
  revision: number;
  updated_at: string;
}

export type CapacitySettingsUpdate = Omit<
  CapacitySettings,
  "updated_at"
>;

export interface EngineFilters {
  limit?: number;
  offset?: number;
  kind?: string;
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
  getWorkflowSettings(signal?: AbortSignal): Promise<WorkflowSettings>;
  updateWorkflowSettings(
    payload: WorkflowSettingsUpdate,
  ): Promise<WorkflowSettings>;
  getCapacitySettings(signal?: AbortSignal): Promise<CapacitySettings>;
  updateCapacitySettings(
    payload: CapacitySettingsUpdate,
  ): Promise<CapacitySettings>;
}
