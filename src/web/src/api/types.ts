export type SupportedEngineKind = "asr" | "vad";
export type EngineKind = SupportedEngineKind | (string & {});
export type EngineApi = "ux_speech" | "ux_vad" | (string & {});
export type EngineProtocol = "grpc" | (string & {});
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

export type FailurePolicy = "fail_pipeline" | "fail_open";

export interface NodePosition {
  x: number;
  y: number;
}

export interface PipelineNode {
  id: string;
  type: string;
  display_name: string | null;
  engine_id: string | null;
  config: Record<string, unknown>;
  failure_policy: FailurePolicy | null;
  position: NodePosition;
}

export interface PipelineEdge {
  id: string;
  source_node: string;
  source_port: string;
  target_node: string;
  target_port: string;
}

export interface PipelineDefinition {
  schema_version: 1;
  nodes: PipelineNode[];
  edges: PipelineEdge[];
}

export interface PipelineDraft {
  definition: PipelineDefinition;
  revision: number;
  updated_at: string;
  active_revision: number | null;
}

export interface PipelineRevision {
  definition: PipelineDefinition;
  revision: number;
  activated_at: string;
  engine_revisions: Record<string, number>;
}

export interface PipelineDiagnostic {
  code: string;
  message: string;
  node_id: string | null;
  edge_id: string | null;
  port: string | null;
}

export interface PipelineValidation {
  valid: boolean;
  diagnostics: PipelineDiagnostic[];
  topological_order: string[];
}

export interface NodePortDefinition {
  payload_type: string;
  required: boolean;
  aggregate: boolean;
}

export interface NodeTypeDefinition {
  type: string;
  display_name: string;
  input_ports: Record<string, NodePortDefinition>;
  output_ports: Record<string, NodePortDefinition>;
  config_schema: Record<string, unknown>;
  allowed_failure_policies: FailurePolicy[];
  default_failure_policy: FailurePolicy;
  required_api: EngineApi | null;
  max_instances: number | null;
  source: boolean;
  sink: boolean;
  version: number;
}

export interface HealthNodeStatus {
  node_id: string;
  node_type: string;
  state: string;
  engine_id: string | null;
  reason: string | null;
}

export interface HealthStatus {
  status: string;
  database: string;
  data_plane: string;
  problems: string[];
  pipeline_revision: number | null;
  nodes: HealthNodeStatus[];
}

export interface WorkflowSettings {
  min_chunk_bytes: number;
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
  getEngine(
    engineId: string,
    signal?: AbortSignal,
  ): Promise<EngineAsset>;
  createEngine(payload: EngineWritePayload): Promise<EngineAsset>;
  updateEngine(
    engineId: string,
    payload: Partial<EngineWritePayload>,
  ): Promise<EngineAsset>;
  deleteEngine(engineId: string): Promise<void>;
  listNodeTypes(signal?: AbortSignal): Promise<NodeTypeDefinition[]>;
  getPipeline(signal?: AbortSignal): Promise<PipelineDraft>;
  updatePipelineDraft(
    definition: PipelineDefinition,
    expectedRevision: number,
  ): Promise<PipelineDraft>;
  validatePipeline(): Promise<PipelineValidation>;
  activatePipeline(expectedRevision: number): Promise<PipelineRevision>;
  getActivePipeline(signal?: AbortSignal): Promise<PipelineRevision>;
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
