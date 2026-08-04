export type SupportedEngineKind = "asr" | "vad" | "llm" | "tts";
export type EngineKind = SupportedEngineKind | (string & {});
export type EngineApi =
  | "ux_speech"
  | "ux_vad"
  | "responses"
  | "qwen_tts_duplex"
  | (string & {});
export type EngineProtocol = "grpc" | "https" | (string & {});
export type EngineStatus =
  | "observed_running"
  | "confirmed_running"
  | "unavailable"
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
  credential_env?: string | null;
  api_key?: string | null;
  enabled: boolean;
  status: EngineStatus;
  image: string | null;
  description: string | null;
  status_basis: string | null;
  status_observed_at: string | null;
  last_success_at?: string | null;
  probe_latency_ms?: number | null;
  consecutive_failures?: number;
  next_probe_at?: string | null;
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

export interface EngineValidationResult {
  engine_id: string;
  name: string;
  revision: number;
  outcome: "passed" | "failed" | "stale";
  message: string | null;
}

export interface EngineValidation {
  results: EngineValidationResult[];
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
  credential_env?: string | null;
  api_key?: string | null;
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

export type WorkflowKind = "transcription" | "voice_agent";

export interface WorkflowSummary {
  id: string;
  slug: string;
  name: string;
  kind: WorkflowKind;
  description: string | null;
  draft_revision: number;
  active_revision: number | null;
  created_at: string;
  updated_at: string;
}

export interface WorkflowCreatePayload {
  slug: string;
  name: string;
  kind: WorkflowKind;
  description: string | null;
  definition: PipelineDefinition;
}

export interface PhysicalVadConfig {
  min_segment_seconds: number;
  max_segment_seconds: number;
  audio_buffer_seconds: number;
  speech_pre_roll_seconds: number;
}

export interface ResponsesAgentConfig {
  model: string;
  thinking_enabled: boolean;
  instructions: string;
  max_output_tokens: number;
  max_tool_rounds: number;
  max_parallel_tools: number;
  tool_timeout_seconds: number;
  service_tools: string[];
  mcp_servers: Array<{
    server_label: string;
    server_url: string;
    require_approval: "always" | "never";
    allowed_tools: string[] | null;
    headers_env: Record<string, string>;
  }>;
  history_max_items: number;
}

export interface DuplexTtsConfig {
  default_voice: string;
  allowed_voices: string[];
  language: string;
  decoder_chunk_size: number;
}

export interface StageBinding<T> {
  engine_id: string | null;
  config: T;
}

export interface StageWorkflowDefinition {
  schema_version: 2;
  stages: {
    physical_vad: StageBinding<PhysicalVadConfig>;
    semantic_vad: (StageBinding<Record<string, never>> & {
      failure_policy: FailurePolicy;
    }) | null;
    asr: StageBinding<Record<string, never>>;
    responses_agent: StageBinding<ResponsesAgentConfig> | null;
    duplex_tts: StageBinding<DuplexTtsConfig> | null;
  };
}

export interface ManagedWorkflowDraft {
  workflow_id: string;
  definition: StageWorkflowDefinition;
  revision: number;
  updated_at: string;
  active_revision: number | null;
}

export interface ManagedWorkflowCreatePayload {
  slug: string;
  name: string;
  kind: WorkflowKind;
  description: string | null;
}

export interface WorkflowStageStatus {
  stage: string;
  engine_id: string | null;
  engine_name: string | null;
  health: string;
  reason: string | null;
  checked_at: string | null;
}

export interface WorkflowStatus {
  workflow_id: string;
  state: string;
  draft_revision: number;
  active_revision: number | null;
  has_unpublished_changes: boolean;
  public_models: string[];
  diagnostics: Array<PipelineDiagnostic & { stage?: string | null; field?: string | null }>;
  stages: WorkflowStageStatus[];
}

export interface RuntimeLimiterStatus {
  active: number;
  capacity: number;
  rejected: number;
}

export interface RuntimeStatus {
  grpc: RuntimeLimiterStatus;
  realtime: RuntimeLimiterStatus;
  webrtc: RuntimeLimiterStatus;
  asr: RuntimeLimiterStatus;
  responses: RuntimeLimiterStatus;
  tts: RuntimeLimiterStatus;
  started_at: string;
}

export interface TestSession {
  id: string;
  workflow_id: string;
  model: string;
  protocol: "wss" | "webrtc";
  endpoint: string;
  expires_at: string;
}

export interface ModelRoute {
  model: string;
  workflow_id: string;
  visibility: "public" | "internal";
  updated_at: string;
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
  max_responses_concurrency?: number;
  max_tts_concurrency?: number;
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
  validateEngines?(): Promise<EngineValidation>;
  listNodeTypes(signal?: AbortSignal): Promise<NodeTypeDefinition[]>;
  getPipeline(signal?: AbortSignal, workflowId?: string): Promise<PipelineDraft>;
  updatePipelineDraft(
    definition: PipelineDefinition,
    expectedRevision: number,
    workflowId?: string,
  ): Promise<PipelineDraft>;
  validatePipeline(workflowId?: string): Promise<PipelineValidation>;
  activatePipeline(expectedRevision: number, workflowId?: string): Promise<PipelineRevision>;
  getActivePipeline(signal?: AbortSignal, workflowId?: string): Promise<PipelineRevision>;
  listWorkflows?(signal?: AbortSignal): Promise<WorkflowSummary[]>;
  createWorkflow?(payload: WorkflowCreatePayload): Promise<PipelineDraft>;
  deleteWorkflow?(workflowId: string): Promise<void>;
  listModelRoutes?(signal?: AbortSignal): Promise<ModelRoute[]>;
  putModelRoute?(route: Omit<ModelRoute, "updated_at">): Promise<ModelRoute>;
  deleteModelRoute?(model: string): Promise<void>;
  getReadiness(signal?: AbortSignal): Promise<HealthStatus>;
  getWorkflowSettings(signal?: AbortSignal): Promise<WorkflowSettings>;
  updateWorkflowSettings(
    payload: WorkflowSettingsUpdate,
  ): Promise<WorkflowSettings>;
  getCapacitySettings(signal?: AbortSignal): Promise<CapacitySettings>;
  updateCapacitySettings(
    payload: CapacitySettingsUpdate,
  ): Promise<CapacitySettings>;
  createManagedWorkflow?(payload: ManagedWorkflowCreatePayload): Promise<ManagedWorkflowDraft>;
  getManagedWorkflowDraft?(workflowId: string, signal?: AbortSignal): Promise<ManagedWorkflowDraft>;
  saveManagedWorkflowDraft?(
    workflowId: string,
    definition: StageWorkflowDefinition,
    expectedRevision: number,
  ): Promise<ManagedWorkflowDraft>;
  publishWorkflow?(workflowId: string, expectedRevision: number): Promise<PipelineRevision>;
  getWorkflowStatus?(workflowId: string, signal?: AbortSignal): Promise<WorkflowStatus>;
  listWorkflowRevisions?(workflowId: string, signal?: AbortSignal): Promise<PipelineRevision[]>;
  restoreWorkflowRevision?(
    workflowId: string,
    revision: number,
    expectedRevision: number,
  ): Promise<ManagedWorkflowDraft>;
  patchWorkflow?(workflowId: string, payload: { name?: string; description?: string | null }): Promise<ManagedWorkflowDraft>;
  probeEngine?(engineId: string): Promise<EngineValidationResult>;
  getEngineUsage?(engineId: string, signal?: AbortSignal): Promise<{ engine_id: string; references: string[] }>;
  getRuntimeStatus?(signal?: AbortSignal): Promise<RuntimeStatus>;
  createTestSession?(workflowId: string, protocol: "wss" | "webrtc"): Promise<TestSession>;
}
