import type { components } from "./generated";

type Schemas = components["schemas"];

export type EngineKind = Schemas["EngineKind"];
export type EngineApi = Schemas["EngineApi"];
export type EngineProtocol = Schemas["EngineProtocol"];
export type EngineStatus = Schemas["EngineStatus"];
export type EngineAsset = Schemas["EngineRead"];
export type EngineListResponse = Schemas["EngineList"];
export type EngineValidation = Schemas["EngineValidationRead"];
export type EngineValidationResult = Schemas["EngineValidationResultRead"];
export type EngineModelList = Schemas["EngineModelListRead"];
export type EngineWritePayload = Schemas["EngineCreate"];
export type EngineFilters = {
  limit?: number;
  offset?: number;
  kind?: EngineKind;
  api?: EngineApi;
  enabled?: boolean;
  q?: string;
};

export type FailurePolicy = Schemas["FailurePolicy"];
export type AudioWorkflowDocument = Schemas["AudioWorkflowDocument"];
export type AudioNode = AudioWorkflowDocument["nodes"][number];
export type AudioEdge = Schemas["AudioEdge"];
export type NamedOutputBinding = Schemas["NamedOutputBinding"];
export type NodePosition = Schemas["NodePosition"];
export type AudioNodeType = Schemas["AudioNodeTypeRead"];
export type AudioWorkflow = Schemas["AudioWorkflowRead"];
export type AudioWorkflowDraft = Schemas["AudioWorkflowDraftRead"];
export type AudioWorkflowRevision = Schemas["AudioWorkflowRevisionRead"];
export type AudioWorkflowValidation = Schemas["AudioWorkflowValidationRead"];
export type GraphDiagnostic = Schemas["GraphDiagnosticRead"];
export type AudioWorkflowCreate = Schemas["AudioWorkflowCreate"];

export type VoiceAgent = Schemas["VoiceAgentRead"];
export type VoiceAgentDraft = Schemas["VoiceAgentDraftRead"];
export type VoiceAgentDefinition = Schemas["VoiceAgentDefinition"];
export type VoiceAgentRevision = Schemas["VoiceAgentRevisionRead"];
export type VoiceAgentValidation = Schemas["VoiceAgentValidationRead"];
export type RealtimeRoute = Schemas["RealtimeRouteRead"];
export type RealtimeRouteUpsert = Schemas["RealtimeRouteUpsert"];
export type RealtimeSessionKind = "transcription" | "voice_agent";

type GeneratedWorkflowSettings = Schemas["WorkflowSettingsRead"];
export type WorkflowSettings = GeneratedWorkflowSettings;
export type WorkflowSettingsUpdate = Schemas["WorkflowSettingsUpdate"];

type GeneratedCapacitySettings = Schemas["CapacitySettingsRead"];
export type CapacitySettings = Omit<
  GeneratedCapacitySettings,
  "grpc" | "realtime" | "webrtc"
> & {
  grpc: Schemas["ProtocolCapacityConfig"];
  realtime: Schemas["ProtocolCapacityConfig"];
  webrtc: Schemas["ProtocolCapacityConfig"];
};
export type CapacitySettingsUpdate = Omit<
  Schemas["CapacitySettingsUpdate"],
  "max_embedding_concurrency" | "max_qdrant_concurrency" | "max_responses_concurrency" | "max_tts_concurrency"
> & {
  max_embedding_concurrency?: number;
  max_qdrant_concurrency?: number;
  max_responses_concurrency?: number;
  max_tts_concurrency?: number;
};
export type RuntimeStatus = Schemas["RuntimeStatusRead"];
export type RuntimeOverview = Schemas["RuntimeOverviewRead"];

export interface NamedVector { name: string; size: number; distance: "cosine" | "euclid" | "dot" | "manhattan"; }
export interface VectorSpace { collection_name: string; vectors: NamedVector[]; template: "voiceprint" | null; }
export interface VectorInfrastructure { configured: boolean; ready: boolean; grpc_target: string | null; http_url: string | null; }
export interface VectorPoint { point_id: string; payload: Record<string, unknown>; }
export interface VectorMatch extends VectorPoint { score: number; }

export interface AudioHarnessApi {
  listEngines(filters?: EngineFilters, signal?: AbortSignal): Promise<EngineListResponse>;
  getEngine(engineId: string, signal?: AbortSignal): Promise<EngineAsset>;
  createEngine(payload: EngineWritePayload): Promise<EngineAsset>;
  updateEngine(engineId: string, payload: Partial<EngineWritePayload>): Promise<EngineAsset>;
  deleteEngine(engineId: string): Promise<void>;
  validateEngines(): Promise<EngineValidation>;
  probeEngine(engineId: string): Promise<EngineValidationResult>;
  getEngineUsage(engineId: string, signal?: AbortSignal): Promise<Schemas["EngineUsageRead"]>;
  listEngineModels(engineId: string, signal?: AbortSignal): Promise<EngineModelList>;

  listAudioNodeTypes(signal?: AbortSignal): Promise<AudioNodeType[]>;
  listAudioWorkflows(signal?: AbortSignal): Promise<AudioWorkflow[]>;
  createAudioWorkflow(payload: AudioWorkflowCreate): Promise<AudioWorkflow>;
  patchAudioWorkflow(workflowId: string, payload: Schemas["AudioWorkflowPatch"]): Promise<AudioWorkflow>;
  deleteAudioWorkflow(workflowId: string): Promise<void>;
  getAudioWorkflowDraft(workflowId: string, signal?: AbortSignal): Promise<AudioWorkflowDraft>;
  saveAudioWorkflowDraft(workflowId: string, document: AudioWorkflowDocument, expectedRevision: number): Promise<AudioWorkflowDraft>;
  validateAudioWorkflow(workflowId: string): Promise<AudioWorkflowValidation>;
  publishAudioWorkflow(workflowId: string, expectedRevision: number): Promise<AudioWorkflowRevision>;
  getActiveAudioWorkflow(workflowId: string, signal?: AbortSignal): Promise<AudioWorkflowRevision>;
  listAudioWorkflowRevisions(workflowId: string, signal?: AbortSignal): Promise<AudioWorkflowRevision[]>;
  restoreAudioWorkflowRevision(workflowId: string, revision: number, expectedRevision: number): Promise<AudioWorkflowDraft>;

  listVoiceAgents(signal?: AbortSignal): Promise<VoiceAgent[]>;
  createVoiceAgent(payload: Schemas["VoiceAgentCreate"]): Promise<VoiceAgent>;
  getVoiceAgentDraft(profileId: string, signal?: AbortSignal): Promise<VoiceAgentDraft>;
  saveVoiceAgentDraft(profileId: string, definition: VoiceAgentDefinition, expectedRevision: number): Promise<VoiceAgentDraft>;
  validateVoiceAgent(profileId: string): Promise<VoiceAgentValidation>;
  publishVoiceAgent(profileId: string, expectedRevision: number): Promise<VoiceAgentRevision>;
  listVoiceAgentRevisions(profileId: string, signal?: AbortSignal): Promise<VoiceAgentRevision[]>;
  restoreVoiceAgentRevision(profileId: string, revision: number, expectedRevision: number): Promise<VoiceAgentDraft>;
  listRealtimeRoutes(signal?: AbortSignal): Promise<RealtimeRoute[]>;
  getRealtimeRoute(model: string, signal?: AbortSignal): Promise<RealtimeRoute>;
  putRealtimeRoute(route: RealtimeRouteUpsert): Promise<RealtimeRoute>;
  deleteRealtimeRoute(model: string): Promise<void>;

  getRuntimeOverview(signal?: AbortSignal): Promise<RuntimeOverview>;
  getWorkflowSettings(signal?: AbortSignal): Promise<WorkflowSettings>;
  updateWorkflowSettings(payload: WorkflowSettingsUpdate): Promise<WorkflowSettings>;
  setGrpcAudioWorkflow(workflowId: string, expectedRevision: number): Promise<WorkflowSettings>;
  getCapacitySettings(signal?: AbortSignal): Promise<CapacitySettings>;
  updateCapacitySettings(payload: CapacitySettingsUpdate): Promise<CapacitySettings>;
  getRuntimeStatus(signal?: AbortSignal): Promise<RuntimeStatus>;
  getVectorInfrastructure(signal?: AbortSignal): Promise<VectorInfrastructure>;
  listVectorSpaces(signal?: AbortSignal): Promise<VectorSpace[]>;
  createVectorSpace(payload: Record<string, unknown>): Promise<VectorSpace>;
  destroyVectorSpace(collectionName: string, confirmation: string): Promise<void>;
  registerVectorAudio(id: string, form: FormData): Promise<{ point_id: string }>;
  searchVectorAudio(id: string, form: FormData): Promise<{ embedding: number[]; matches: VectorMatch[] }>;
  scrollVectorPoints(id: string, payload: Record<string, unknown>): Promise<{ items: VectorPoint[]; next_offset: string | null }>;
  replaceVectorPayload(id: string, pointId: string, payload: Record<string, unknown>): Promise<void>;
  deleteVectorPoint(id: string, pointId: string): Promise<void>;
}
