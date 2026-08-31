import { useEffect, useRef, useState } from "react";

import { errorMessage } from "../api/client";
import type { AudioHarnessApi, RealtimeSessionKind } from "../api/types";
import { PcmPlayer, startMicrophone, type MicrophoneCapture } from "../realtimeAudio";

interface Props {
  api: AudioHarnessApi;
  model: string;
  workflowKind: RealtimeSessionKind;
  canTest: boolean;
  blockedReason?: string;
}

interface EventLine {
  id: string;
  type: string;
  time: string;
  detail: string;
  count: number;
}

export interface StreamEventSummary {
  key: string;
  type: "x_aha.vad.progress" | "response.output_audio.delta";
  status: "streaming" | "completed";
  time: string;
  detail: string;
  count: number;
}

interface Latencies {
  interim?: number;
  transcription?: number;
  model?: number;
  audio?: number;
}

interface VadProgressView {
  processedAudioMs: number;
  final: boolean;
}

interface FinalClusterView {
  status: "completed" | "failed";
  revision: number;
  assignments: number;
}

interface VectorSearchMatchView {
  speakerId: string;
  score: number;
}

interface BackendToolView {
  callId: string;
  name: string;
  status: "running" | "completed" | "failed";
  detail: string;
}

interface BackendActivityView {
  sessionId: string | null;
  status: "idle" | "running" | "summarizing" | "failed" | "cancelled";
  text: string;
  tools: BackendToolView[];
  message: string | null;
}

interface BackendApprovalView {
  requestId: string;
  approvalId: string;
  toolName: string;
  reason: string;
  submitting: boolean;
}

interface BackendQuestionItem {
  id: string;
  question: string;
  options: Array<{ label: string; description?: string }>;
  multiSelect: boolean;
}

interface BackendQuestionView {
  requestId: string;
  questions: BackendQuestionItem[];
  selections: Record<string, string[]>;
  custom: Record<string, string>;
  submitting: boolean;
}

const EMPTY_BACKEND_ACTIVITY: BackendActivityView = {
  sessionId: null,
  status: "idle",
  text: "",
  tools: [],
  message: null,
};

export interface VectorSearchEventView {
  status: "completed" | "failed";
  groups: VectorSearchMatchView[];
  error: string | null;
}

export interface SpeakerFilterEventView {
  status: "matched" | "rejected" | "failed";
  speakerId: string | null;
  score: number | null;
  reason: string | null;
  error: string | null;
  audioStartMs: number | null;
  audioEndMs: number | null;
}

const SPEAKER_FILTER_REASON_LABELS: Record<string, string> = {
  no_match: "未检索到匹配说话人",
  speaker_not_allowed: "说话人不在允许列表",
  score_too_low: "匹配分数低于门槛",
};

export type TranscriptViewPhase = "idle" | "listening" | "interim" | "processing" | "primary" | "final";

export interface TranscriptView {
  phase: TranscriptViewPhase;
  text: string;
  itemId: string | null;
}

const EMPTY_TRANSCRIPT_VIEW: TranscriptView = { phase: "idle", text: "", itemId: null };

export function nextTranscriptView(
  current: TranscriptView,
  event: Record<string, unknown>,
): TranscriptView {
  const type = String(event.type ?? "");
  const itemId = String(event.item_id ?? "") || null;
  const currentHasTranscript = current.phase === "interim" || current.phase === "primary";
  const belongsToOlderItem = currentHasTranscript
    && current.itemId !== null
    && itemId !== null
    && current.itemId !== itemId;
  if (type === "input_audio_buffer.speech_started") {
    return current.phase === "interim" && current.itemId === itemId
      ? current
      : { phase: "listening", text: "正在聆听…", itemId };
  }
  if (type === "x_aha.input_audio_transcription.interim") {
    const text = String(event.transcript ?? "");
    return text ? { phase: "interim", text, itemId } : current;
  }
  if (type === "x_aha.input_audio_transcription.interim_cleared") {
    return current.phase === "interim" && !belongsToOlderItem
      ? { phase: "processing", text: "正在生成最终转写…", itemId: current.itemId }
      : current;
  }
  if (type === "input_audio_buffer.speech_stopped") {
    if (belongsToOlderItem) return current;
    return current.phase === "interim" || current.phase === "primary"
      ? current
      : { phase: "processing", text: "正在生成最终转写…", itemId: itemId ?? current.itemId };
  }
  if (type === "conversation.item.input_audio_transcription.delta") {
    if (belongsToOlderItem) return current;
    const delta = String(event.delta ?? "");
    if (!delta) return current;
    return {
      phase: "primary",
      text: current.phase === "primary" ? current.text + delta : delta,
      itemId: itemId ?? current.itemId,
    };
  }
  if (type === "conversation.item.input_audio_transcription.completed") {
    if (belongsToOlderItem) return current;
    return { phase: "final", text: String(event.transcript ?? ""), itemId: itemId ?? current.itemId };
  }
  return current;
}

export function completedTranscriptIsSuperseded(
  completedItemId: string | null,
  interimItemId: string | null,
  interimRevision: number,
): boolean {
  return interimRevision > 0
    && completedItemId !== null
    && interimItemId !== null
    && completedItemId !== interimItemId;
}

export function speakerFilterReasonLabel(reason: unknown): string {
  const code = String(reason ?? "");
  return (SPEAKER_FILTER_REASON_LABELS[code] ?? code) || "未知拒绝原因";
}

function eventAudioRange(event: Record<string, unknown>): string {
  return `${String(event.audio_start_ms ?? "—")}–${String(event.audio_end_ms ?? "—")} ms`;
}

export function eventDetail(event: Record<string, unknown>): string {
  if (event.type === "response.output_audio.delta") {
    return "音频数据已省略";
  }
  if (event.type === "x_aha.vad.progress") {
    return event.final === true
      ? `VAD 处理完成 · ${String(event.processed_audio_ms ?? "—")} ms`
      : `VAD 处理中 · ${String(event.processed_audio_ms ?? "—")} ms`;
  }
  if (event.type === "x_aha.vad.physical_speech_started") {
    return `物理语音开始 ${String(event.audio_start_ms)} ms · ${String(event.physical_segment_id ?? "—")}`;
  }
  if (event.type === "x_aha.vad.physical_speech_stopped") {
    return `物理语音结束 ${String(event.audio_start_ms)}–${String(event.audio_end_ms)} ms · ${String(event.physical_segment_id ?? "—")}`;
  }
  if (event.type === "x_aha.vad.turn_completed") {
    return `VAD 轮次完成 ${String(event.audio_start_ms)}–${String(event.audio_end_ms)} ms · ${String((event.physical_segment_ids as unknown[] | undefined)?.length ?? 0)} 个物理段`;
  }
  if (event.type === "x_aha.input_audio_transcription.interim") {
    return `预览 r${String(event.revision)} · ${String(event.audio_start_ms)}–${String(event.audio_end_ms)} ms · ${String(event.transcript ?? "")}`;
  }
  if (event.type === "x_aha.input_audio_transcription.interim_cleared") {
    return `中间结果已清除：${String(event.reason ?? "unknown")}`;
  }
  if (event.type === "x_aha.agent_backend.session") {
    return `DSH session ${String(event.backend_session_id ?? "—")} · ${String(event.status ?? "unknown")}`;
  }
  if (event.type === "x_aha.agent_backend.turn.started") return "DSH 开始执行任务";
  if (event.type === "x_aha.agent_backend.turn.completed") return "DSH 执行结束，等待 Responses 总结";
  if (event.type === "x_aha.agent_backend.text.delta") return String(event.delta ?? "");
  if (event.type === "x_aha.agent_backend.tool.started") return `${String(event.name ?? "工具")} · ${String(event.arguments ?? "")}`;
  if (event.type === "x_aha.agent_backend.tool.completed") return String(event.error ?? event.result ?? "工具执行完成");
  if (event.type === "x_aha.agent_backend.approval.requested") return `${String(event.tool_name ?? "工具")} 请求审批：${String(event.reason ?? "未提供原因")}`;
  if (event.type === "x_aha.agent_backend.approval.resolved") return `审批 ${String(event.resolution ?? "handled")} · ${String(event.outcome ?? "—")}`;
  if (event.type === "x_aha.agent_backend.question.requested") return `DSH 提出 ${Array.isArray(event.questions) ? event.questions.length : 0} 个问题`;
  if (event.type === "x_aha.agent_backend.question.resolved") return `问题 ${String(event.resolution ?? "handled")} · ${String(event.outcome ?? "—")}`;
  if (event.type === "x_aha.agent_backend.failed" || event.type === "x_aha.agent_backend.cancelled") return String(event.message ?? "Agent Backend 已结束");
  if (event.type === "x_aha.vector.search.completed") {
    const groups = Array.isArray(event.groups) ? event.groups.length : 0;
    return `${groups} 个候选人 · ${String(event.latency_ms ?? "—")} ms`;
  }
  if (event.type === "x_aha.vector.search.failed") {
    return String((event.error as { message?: string } | undefined)?.message ?? "人物搜索失败");
  }
  if (event.type === "x_aha.speaker_filter.matched") {
    const score = Number(event.score);
    return `已放行 ${String(event.speaker_id ?? "未提供 Speaker ID")} · 分数 ${Number.isFinite(score) ? score.toFixed(4) : "—"} · ${eventAudioRange(event)}`;
  }
  if (event.type === "x_aha.speaker_filter.rejected") {
    return `已拒绝 · ${speakerFilterReasonLabel(event.reason)} · ${eventAudioRange(event)}`;
  }
  if (event.type === "x_aha.speaker_filter.failed") {
    const message = String((event.error as { message?: string } | undefined)?.message ?? "说话人过滤失败");
    return `过滤失败 · ${message} · ${eventAudioRange(event)}`;
  }
  if (event.type === "x_aha.diarization.local.delta") {
    const segments = Array.isArray(event.segments) ? event.segments.length : 0;
    return `局部分离增量 #${String(event.sequence ?? 0)} · ${segments} 个片段`;
  }
  if (event.type === "x_aha.diarization.local.completed") {
    const segments = Array.isArray(event.segments) ? event.segments.length : 0;
    return `局部分离完成 · ${segments} 个片段 · ${String(event.latency_ms ?? "—")} ms`;
  }
  if (event.type === "x_aha.diarization.local.failed") {
    return String((event.error as { message?: string } | undefined)?.message ?? "说话人分离失败");
  }
  if (event.type === "x_aha.input_audio_transcription.speaker.delta") {
    return `说话人 ${String(event.local_speaker_id ?? "未识别")} · ${String(event.delta ?? "")}`;
  }
  if (event.type === "x_aha.input_audio_transcription.speaker.completed") {
    return `说话人 ${String(event.local_speaker_id ?? "未识别")} · ${String(event.transcript ?? "")}`;
  }
  if (event.type === "x_aha.input_audio_transcription.speaker.failed") {
    return String((event.error as { message?: string } | undefined)?.message ?? "说话人片段转写失败");
  }
  if (event.type === "x_aha.diarization.cluster.completed") {
    const assignments = Array.isArray(event.assignments) ? event.assignments.length : 0;
    if (event.final === true) {
      return `最终聚类快照 r${String(event.revision)} · ${assignments} 个片段 · 已处理 ${String(event.processed_audio_ms ?? "—")} ms`;
    }
    return `聚类快照 r${String(event.revision)} · ${assignments} 个片段`;
  }
  if (event.type === "x_aha.diarization.cluster.failed") {
    const message = String((event.error as { message?: string } | undefined)?.message ?? "说话人聚类失败");
    return event.final === true
      ? `最终聚类失败 · 已处理 ${String(event.processed_audio_ms ?? "—")} ms · ${message}`
      : message;
  }
  return String(
    event.delta
      ?? event.transcript
      ?? (event.error as { message?: string } | undefined)?.message
      ?? "",
  );
}

export function vectorSearchEventView(
  event: Record<string, unknown>,
  time: string,
): VectorSearchEventView | null {
  void time;
  const type = String(event.type ?? "");
  if (type !== "x_aha.vector.search.completed" && type !== "x_aha.vector.search.failed") return null;
  const groups = Array.isArray(event.groups)
    ? event.groups.flatMap((group) => {
        if (typeof group !== "object" || group === null) return [];
        const value = group as Record<string, unknown>;
        const score = Number(value.score);
        return [{
          speakerId: String(value.speaker_id ?? ""),
          score: Number.isFinite(score) ? score : 0,
        }];
      })
    : [];
  return {
    status: type.endsWith(".completed") ? "completed" : "failed",
    groups,
    error: type.endsWith(".failed")
      ? String((event.error as { message?: string } | undefined)?.message ?? "人物搜索失败")
      : null,
  };
}

export function speakerFilterEventView(
  event: Record<string, unknown>,
): SpeakerFilterEventView | null {
  const type = String(event.type ?? "");
  if (
    type !== "x_aha.speaker_filter.matched"
    && type !== "x_aha.speaker_filter.rejected"
    && type !== "x_aha.speaker_filter.failed"
  ) return null;
  const status = type.slice(type.lastIndexOf(".") + 1) as SpeakerFilterEventView["status"];
  const score = Number(event.score);
  const audioStartMs = Number(event.audio_start_ms);
  const audioEndMs = Number(event.audio_end_ms);
  return {
    status,
    speakerId: status === "matched" ? String(event.speaker_id ?? "") || null : null,
    score: status === "matched" && Number.isFinite(score) ? score : null,
    reason: status === "rejected" ? speakerFilterReasonLabel(event.reason) : null,
    error: status === "failed"
      ? String((event.error as { message?: string } | undefined)?.message ?? "说话人过滤失败")
      : null,
    audioStartMs: Number.isFinite(audioStartMs) ? audioStartMs : null,
    audioEndMs: Number.isFinite(audioEndMs) ? audioEndMs : null,
  };
}

function waitForIce(peer: RTCPeerConnection): Promise<void> {
  if (peer.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const changed = () => {
      if (peer.iceGatheringState === "complete") {
        peer.removeEventListener("icegatheringstatechange", changed);
        resolve();
      }
    };
    peer.addEventListener("icegatheringstatechange", changed);
  });
}

function displayMs(value: number | undefined, waiting: string): string {
  if (value === undefined) return waiting;
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(2)} s`;
}

export function realtimeSessionUpdate(
  workflowKind: RealtimeSessionKind,
  model: string,
) {
  return {
    type: "session.update",
    session: {
      type: workflowKind === "voice_agent" ? "realtime" : "transcription",
      ...(workflowKind === "transcription"
        ? {
            audio: {
              input: {
                transcription: { model },
              },
            },
          }
        : {}),
    },
  };
}

export function realtimeEndpoint(
  protocol: "wss" | "webrtc",
  workflowKind: RealtimeSessionKind,
  model: string,
  ahaEnabled: boolean,
): string {
  const encodedModel = encodeURIComponent(model);
  const extension = ahaEnabled ? "&x_aha=v1" : "";
  if (protocol === "webrtc") {
    return `/v1/realtime/calls?model=${encodedModel}${extension}`;
  }
  return workflowKind === "transcription"
    ? `/v1/realtime?intent=transcription${extension}`
    : `/v1/realtime?model=${encodedModel}${extension}`;
}

export function isResponseTextDelta(type: string): boolean {
  return type === "response.output_text.delta"
    || type === "response.output_audio_transcript.delta";
}

export function isTtsFirstAudio(type: string): boolean {
  return type === "response.output_audio.delta";
}

function audioSummaryKey(event: Record<string, unknown>): string {
  return `audio:${String(event.response_id ?? "unattributed") || "unattributed"}`;
}

export function isStreamSummaryEvent(type: string): boolean {
  return type === "x_aha.vad.progress" || type === "response.output_audio.delta";
}

export function updateStreamSummaries(
  current: StreamEventSummary[],
  event: Record<string, unknown>,
  time: string,
): StreamEventSummary[] {
  const type = String(event.type ?? "");
  if (type === "response.output_audio.done") {
    const key = audioSummaryKey(event);
    return current.map((summary) => summary.key === key
      ? { ...summary, status: "completed", time }
      : summary);
  }
  if (!isStreamSummaryEvent(type)) return current;
  const summaryType: StreamEventSummary["type"] = type === "x_aha.vad.progress"
    ? "x_aha.vad.progress"
    : "response.output_audio.delta";
  const key = summaryType === "x_aha.vad.progress" ? "vad" : audioSummaryKey(event);
  const summary: StreamEventSummary = {
    key,
    type: summaryType,
    status: summaryType === "x_aha.vad.progress" && event.final === true ? "completed" : "streaming",
    time,
    detail: summaryType === "response.output_audio.delta" ? "TTS 音频块" : eventDetail(event),
    count: 1,
  };
  const existing = current.find((item) => item.key === key);
  if (existing) {
    return current.map((item) => item.key === key
      ? { ...summary, count: item.count + 1 }
      : item);
  }
  const next = [summary, ...current];
  const audio = next.filter((item) => item.type === "response.output_audio.delta").slice(0, 10);
  return [...next.filter((item) => item.type === "x_aha.vad.progress"), ...audio];
}

export function backendApprovalResponse(requestId: string, approvalId: string, approved: boolean) {
  return { type: "x_aha.agent_backend.approval.respond", request_id: requestId, approval_id: approvalId, approved };
}

export function backendQuestionResponse(requestId: string, answers: Array<{ id: string; selected: string[]; custom: string | null }>) {
  return { type: "x_aha.agent_backend.question.respond", request_id: requestId, answers };
}

export function backendCancelEvent() {
  return { type: "x_aha.agent_backend.cancel" };
}

export function appendGroupedEvent(
  current: EventLine[],
  event: EventLine,
): EventLine[] {
  const first = current[0];
  if (first?.type === event.type) {
    return [{ ...first, detail: event.detail, count: first.count + 1 }, ...current.slice(1)];
  }
  return [event, ...current].slice(0, 100);
}

export function RealtimeTestPanel({ api, model, workflowKind, canTest, blockedReason }: Props) {
  const [protocol, setProtocol] = useState<"wss" | "webrtc">("wss");
  const [ahaEnabled, setAhaEnabled] = useState(workflowKind === "transcription");
  const [connection, setConnection] = useState<"idle" | "connecting" | "connected" | "error">("idle");
  const [notice, setNotice] = useState("等待开始测试");
  const [transcriptView, setTranscriptView] = useState<TranscriptView>(EMPTY_TRANSCRIPT_VIEW);
  const [assistant, setAssistant] = useState("");
  const [events, setEvents] = useState<EventLine[]>([]);
  const [eventTotal, setEventTotal] = useState(0);
  const [streamSummaries, setStreamSummaries] = useState<StreamEventSummary[]>([]);
  const [vectorSearch, setVectorSearch] = useState<VectorSearchEventView | null>(null);
  const [vectorResultsOpen, setVectorResultsOpen] = useState(true);
  const [speakerFilter, setSpeakerFilter] = useState<SpeakerFilterEventView | null>(null);
  const [vadProgress, setVadProgress] = useState<VadProgressView | null>(null);
  const [finalCluster, setFinalCluster] = useState<FinalClusterView | null>(null);
  const [backendActivity, setBackendActivity] = useState<BackendActivityView>(EMPTY_BACKEND_ACTIVITY);
  const [backendApproval, setBackendApproval] = useState<BackendApprovalView | null>(null);
  const [backendQuestion, setBackendQuestion] = useState<BackendQuestionView | null>(null);
  const [latencies, setLatencies] = useState<Latencies>({});
  const socketRef = useRef<WebSocket | null>(null);
  const dataChannelRef = useRef<RTCDataChannel | null>(null);
  const microphoneRef = useRef<MicrophoneCapture | null>(null);
  const peerRef = useRef<RTCPeerConnection | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const remoteAudioRef = useRef<HTMLAudioElement | null>(null);
  const playerRef = useRef(new PcmPlayer());
  const stoppingRef = useRef(false);
  const speechStoppedAt = useRef<number | null>(null);
  const speechStartedAt = useRef<number | null>(null);
  const interimRevisionRef = useRef(0);
  const interimItemRef = useRef<string | null>(null);
  const transcriptAt = useRef<number | null>(null);
  const modelAt = useRef<number | null>(null);

  const recordEvent = (event: Record<string, unknown>) => {
    const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    const type = String(event.type ?? "unknown");
    setEventTotal((current) => current + 1);
    setStreamSummaries((current) => updateStreamSummaries(current, event, time));
    if (isStreamSummaryEvent(type)) return;
    setEvents((current) => appendGroupedEvent(current, {
      id: String(event.event_id ?? `${event.type}-${performance.now()}`),
      type,
      time,
      detail: eventDetail(event),
      count: 1,
    }));
  };

  const sendClientEvent = (event: Record<string, unknown>): boolean => {
    const payload = JSON.stringify(event);
    if (protocol === "wss" && socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(payload);
      return true;
    }
    if (protocol === "webrtc" && dataChannelRef.current?.readyState === "open") {
      dataChannelRef.current.send(payload);
      return true;
    }
    setNotice("控制事件发送失败：实时连接尚未就绪");
    return false;
  };

  const handleServerEvent = (event: Record<string, unknown>) => {
    recordEvent(event);
    const type = String(event.type ?? "");
    if (type === "x_aha.agent_backend.session") {
      setBackendActivity((current) => ({ ...current, sessionId: String(event.backend_session_id ?? "") || null, status: event.status === "running" ? "running" : current.status }));
    } else if (type === "x_aha.agent_backend.turn.started") {
      setBackendActivity((current) => ({ ...current, status: "running", text: "", tools: [], message: null }));
      setNotice("DSH 正在执行委派任务");
    } else if (type === "x_aha.agent_backend.text.delta") {
      setBackendActivity((current) => ({ ...current, status: "running", text: (current.text + String(event.delta ?? "")).slice(-16_384) }));
    } else if (type === "x_aha.agent_backend.tool.started") {
      const callId = String(event.call_id ?? event.event_id ?? performance.now());
      const tool: BackendToolView = { callId, name: String(event.name ?? "工具"), status: "running", detail: String(event.arguments ?? "") };
      setBackendActivity((current) => ({ ...current, status: "running", tools: [...current.tools.filter((item) => item.callId !== callId), tool].slice(-20) }));
    } else if (type === "x_aha.agent_backend.tool.completed") {
      const callId = String(event.call_id ?? "");
      setBackendActivity((current) => ({ ...current, tools: current.tools.map((tool) => tool.callId === callId ? { ...tool, status: event.error ? "failed" : "completed", detail: String(event.error ?? event.result ?? "") } : tool) }));
    } else if (type === "x_aha.agent_backend.turn.completed") {
      setBackendActivity((current) => ({ ...current, status: "summarizing" }));
      setNotice("DSH 已完成，Responses 正在生成语音总结");
    } else if (type === "x_aha.agent_backend.approval.requested") {
      setBackendApproval({ requestId: String(event.request_id ?? ""), approvalId: String(event.approval_id ?? ""), toolName: String(event.tool_name ?? "后台工具"), reason: String(event.reason ?? ""), submitting: false });
    } else if (type === "x_aha.agent_backend.approval.resolved") {
      setBackendApproval((current) => current?.requestId === String(event.request_id ?? "") ? null : current);
      setNotice(event.resolution === "already_processed" ? "该审批已由另一输入处理" : event.resolution === "timed_out" ? "审批已超时并默认拒绝" : "审批回答已提交");
    } else if (type === "x_aha.agent_backend.question.requested") {
      const questions = Array.isArray(event.questions) ? event.questions.flatMap((raw) => {
        if (!raw || typeof raw !== "object") return [];
        const question = raw as Record<string, unknown>;
        return [{ id: String(question.id ?? ""), question: String(question.question ?? ""), multiSelect: question.multiSelect === true, options: Array.isArray(question.options) ? question.options.flatMap((rawOption) => rawOption && typeof rawOption === "object" ? [{ label: String((rawOption as Record<string, unknown>).label ?? ""), description: String((rawOption as Record<string, unknown>).description ?? "") || undefined }] : []) : [] }];
      }) : [];
      setBackendQuestion({ requestId: String(event.request_id ?? ""), questions, selections: {}, custom: {}, submitting: false });
    } else if (type === "x_aha.agent_backend.question.resolved") {
      setBackendQuestion((current) => current?.requestId === String(event.request_id ?? "") ? null : current);
      setNotice(event.resolution === "already_processed" ? "这些问题已由另一输入回答" : event.resolution === "timed_out" ? "问题已超时取消" : "结构化答案已提交");
    } else if (type === "x_aha.agent_backend.failed" || type === "x_aha.agent_backend.cancelled") {
      setBackendActivity((current) => ({ ...current, status: type.endsWith("failed") ? "failed" : "cancelled", message: String(event.message ?? "Agent Backend 已结束") }));
      setBackendApproval(null);
      setBackendQuestion(null);
    }
    if (type === "x_aha.vad.progress") {
      const processedAudioMs = Number(event.processed_audio_ms);
      if (Number.isFinite(processedAudioMs)) {
        setVadProgress({
          processedAudioMs,
          final: event.final === true,
        });
      }
    }
    if (
      event.final === true
      && (type === "x_aha.diarization.cluster.completed"
        || type === "x_aha.diarization.cluster.failed")
    ) {
      setFinalCluster({
        status: type.endsWith(".completed") ? "completed" : "failed",
        revision: Number(event.revision ?? 0),
        assignments: Array.isArray(event.assignments) ? event.assignments.length : 0,
      });
    }
    const vectorSearchUpdate = vectorSearchEventView(
      event,
      new Date().toLocaleTimeString("zh-CN", { hour12: false }),
    );
    if (vectorSearchUpdate) setVectorSearch(vectorSearchUpdate);
    const speakerFilterUpdate = speakerFilterEventView(event);
    if (speakerFilterUpdate) setSpeakerFilter(speakerFilterUpdate);
    if (type === "input_audio_buffer.speech_started") {
      speechStartedAt.current = performance.now();
      speechStoppedAt.current = null;
      transcriptAt.current = null;
      modelAt.current = null;
      setNotice(workflowKind === "voice_agent" ? "检测到用户语音；旧回复已中断" : "检测到用户语音，正在接收音频");
      playerRef.current.clear();
      setTranscriptView((current) => nextTranscriptView(current, event));
      const startedItemId = String(event.item_id ?? "") || null;
      if (interimItemRef.current !== startedItemId) interimRevisionRef.current = 0;
      interimItemRef.current = startedItemId;
      setAssistant("");
      setLatencies({});
    } else if (type === "x_aha.vad.physical_speech_started") {
      setNotice("检测到物理语音，正在接收本段音频");
    } else if (type === "x_aha.vad.physical_speech_stopped") {
      setNotice("物理语音已停止，正在等待语义轮次结果");
    } else if (type === "x_aha.vad.turn_completed") {
      setNotice("语义 VAD 轮次已确定，正在提交正式转写");
    } else if (type === "x_aha.input_audio_transcription.interim") {
      const itemId = String(event.item_id ?? "");
      const revision = Number(event.revision ?? 0);
      if (
        itemId
        && revision > 0
        && (interimItemRef.current === null || interimItemRef.current === itemId)
        && revision > interimRevisionRef.current
      ) {
        interimItemRef.current = itemId;
        interimRevisionRef.current = revision;
        setTranscriptView((current) => nextTranscriptView(current, event));
        setNotice("正在生成中间转写，最终结果稍后确认");
        if (speechStartedAt.current !== null) {
          setLatencies((current) => current.interim === undefined
            ? { ...current, interim: performance.now() - speechStartedAt.current! }
            : current);
        }
      }
    } else if (type === "x_aha.input_audio_transcription.interim_cleared") {
      if (interimItemRef.current === String(event.item_id ?? "")) {
        setTranscriptView((current) => nextTranscriptView(current, event));
      }
    } else if (type === "input_audio_buffer.speech_stopped") {
      speechStoppedAt.current = performance.now();
      setTranscriptView((current) => nextTranscriptView(current, event));
      setNotice("话音结束，正在转写");
    } else if (type === "conversation.item.input_audio_transcription.delta") {
      setTranscriptView((current) => nextTranscriptView(current, event));
    } else if (type === "conversation.item.input_audio_transcription.completed") {
      const now = performance.now();
      const completedItemId = String(event.item_id ?? "") || null;
      const superseded = completedTranscriptIsSuperseded(
        completedItemId,
        interimItemRef.current,
        interimRevisionRef.current,
      );
      if (!superseded) {
        transcriptAt.current = now;
        setTranscriptView((current) => nextTranscriptView(current, event));
        if (completedItemId === interimItemRef.current) {
          interimRevisionRef.current = 0;
          interimItemRef.current = null;
        }
        if (speechStoppedAt.current !== null) setLatencies((current) => ({ ...current, transcription: now - speechStoppedAt.current! }));
        setNotice(workflowKind === "voice_agent" ? "转写完成，大模型正在回复" : "转写完成，可以继续说话");
      }
    } else if (isResponseTextDelta(type)) {
      const now = performance.now();
      if (modelAt.current === null) {
        modelAt.current = now;
        if (transcriptAt.current !== null) setLatencies((current) => ({ ...current, model: now - transcriptAt.current! }));
      }
      setAssistant((current) => current + String(event.delta ?? ""));
      setNotice("正在生成并合成语音");
    } else if (isTtsFirstAudio(type)) {
      const now = performance.now();
      if (modelAt.current !== null) setLatencies((current) => current.audio === undefined ? ({ ...current, audio: now - modelAt.current! }) : current);
      if (protocol === "wss" && typeof event.delta === "string") void playerRef.current.push(event.delta);
    } else if (type === "response.output_audio.done" || type === "response.done") {
      setBackendActivity((current) => current.status === "summarizing" ? { ...current, status: "idle" } : current);
      setNotice("回复完成，可以继续说话");
    } else if (type === "error") {
      setNotice(`运行错误：${String((event.error as { message?: string } | undefined)?.message ?? "未知错误")}`);
    }
  };

  const answerApproval = (approved: boolean) => {
    if (!backendApproval) return;
    const sent = sendClientEvent(backendApprovalResponse(backendApproval.requestId, backendApproval.approvalId, approved));
    if (sent) setBackendApproval({ ...backendApproval, submitting: true });
  };

  const toggleQuestionOption = (question: BackendQuestionItem, label: string) => {
    if (!backendQuestion || backendQuestion.submitting) return;
    const current = backendQuestion.selections[question.id] ?? [];
    const selected = question.multiSelect
      ? current.includes(label) ? current.filter((item) => item !== label) : [...current, label]
      : [label];
    setBackendQuestion({ ...backendQuestion, selections: { ...backendQuestion.selections, [question.id]: selected } });
  };

  const answerQuestions = () => {
    if (!backendQuestion) return;
    const answers = backendQuestion.questions.map((question) => ({
      id: question.id,
      selected: backendQuestion.selections[question.id] ?? [],
      custom: backendQuestion.custom[question.id]?.trim() || null,
    }));
    if (sendClientEvent(backendQuestionResponse(backendQuestion.requestId, answers))) {
      setBackendQuestion({ ...backendQuestion, submitting: true });
    }
  };

  const cancelBackend = () => {
    if (sendClientEvent(backendCancelEvent())) setNotice("正在取消 DSH 任务");
  };

  const stop = async () => {
    stoppingRef.current = true;
    socketRef.current?.close();
    socketRef.current = null;
    dataChannelRef.current = null;
    await microphoneRef.current?.stop().catch(() => undefined);
    microphoneRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    peerRef.current?.close();
    peerRef.current = null;
    await playerRef.current.close();
    setConnection("idle");
    setTranscriptView(EMPTY_TRANSCRIPT_VIEW);
    setVectorSearch(null);
    setVectorResultsOpen(true);
    setSpeakerFilter(null);
    setVadProgress(null);
    setFinalCluster(null);
    setBackendActivity(EMPTY_BACKEND_ACTIVITY);
    setBackendApproval(null);
    setBackendQuestion(null);
    interimRevisionRef.current = 0;
    interimItemRef.current = null;
    setNotice("测试已停止；本次文本和音频不会写入数据库");
  };

  useEffect(() => () => {
    socketRef.current?.close();
    void microphoneRef.current?.stop();
    streamRef.current?.getTracks().forEach((track) => track.stop());
    peerRef.current?.close();
    void playerRef.current.close();
  }, []);

  const connectWss = async (endpoint: string) => {
    const url = new URL(endpoint, window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    await new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(url);
      socketRef.current = socket;
      socket.onopen = () => {
        socket.send(JSON.stringify(realtimeSessionUpdate(workflowKind, model)));
        resolve();
      };
      socket.onerror = () => reject(new Error("WSS 握手失败，请检查 TLS、路由和发布状态"));
      socket.onclose = (event) => {
        if (!stoppingRef.current && socketRef.current === socket) {
          setConnection("error");
          const reason = ({ 4404: "Realtime model 路由不存在", 4409: "路由目标尚未发布", 4429: "会话容量已满" } as Record<number, string>)[event.code];
          setNotice(reason ?? `WSS 已关闭（${event.code}）`);
        }
      };
      socket.onmessage = (message) => {
        try { handleServerEvent(JSON.parse(String(message.data)) as Record<string, unknown>); } catch { setNotice("收到无法解析的服务端事件"); }
      };
    });
    microphoneRef.current = await startMicrophone((audio) => {
      if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: "input_audio_buffer.append", audio }));
    });
  };

  const connectWebRtc = async (endpoint: string) => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    streamRef.current = stream;
    const peer = new RTCPeerConnection();
    peerRef.current = peer;
    const channel = peer.createDataChannel("oai-events");
    dataChannelRef.current = channel;
    channel.onopen = () => channel.send(JSON.stringify(realtimeSessionUpdate(workflowKind, model)));
    channel.onmessage = (message) => {
      try { handleServerEvent(JSON.parse(String(message.data)) as Record<string, unknown>); } catch { setNotice("收到无法解析的 DataChannel 事件"); }
    };
    stream.getTracks().forEach((track) => peer.addTrack(track, stream));
    peer.ontrack = (event) => {
      if (remoteAudioRef.current) remoteAudioRef.current.srcObject = event.streams[0] ?? new MediaStream([event.track]);
    };
    peer.onconnectionstatechange = () => {
      if (peer.connectionState === "connected") setNotice("WebRTC 已连接，请开始说话");
      if (
        !stoppingRef.current
        && peerRef.current === peer
        && ["failed", "disconnected", "closed"].includes(peer.connectionState)
      ) {
        setConnection("error");
        setNotice(`WebRTC 状态：${peer.connectionState}`);
      }
    };
    await peer.setLocalDescription(await peer.createOffer());
    await waitForIce(peer);
    if (!peer.localDescription) throw new Error("浏览器没有生成 WebRTC offer");
    const form = new FormData();
    form.set("sdp", peer.localDescription.sdp);
    const response = await fetch(endpoint, { method: "POST", credentials: "same-origin", body: form });
    if (!response.ok) {
      const body = await response.json().catch(() => null) as { error?: { message?: string } } | null;
      throw new Error(body?.error?.message ?? `WebRTC 协商失败（${response.status}）`);
    }
    await peer.setRemoteDescription({ type: "answer", sdp: await response.text() });
  };

  const start = async () => {
    if (!canTest) return;
    stoppingRef.current = false;
    setConnection("connecting");
    setNotice("正在检查资源、路由和麦克风");
    setEvents([]);
    setEventTotal(0);
    setStreamSummaries([]);
    setVectorSearch(null);
    setVectorResultsOpen(true);
    setSpeakerFilter(null);
    setVadProgress(null);
    setFinalCluster(null);
    setBackendActivity(EMPTY_BACKEND_ACTIVITY);
    setBackendApproval(null);
    setBackendQuestion(null);
    setTranscriptView(EMPTY_TRANSCRIPT_VIEW);
    setAssistant("");
    setLatencies({});
    speechStoppedAt.current = null;
    speechStartedAt.current = null;
    interimRevisionRef.current = 0;
    interimItemRef.current = null;
    transcriptAt.current = null;
    modelAt.current = null;
    try {
      const route = await api.getRealtimeRoute(model);
      if (route.visibility !== "public") throw new Error("当前 Realtime model 不是公开路由");
      const endpoint = realtimeEndpoint(protocol, workflowKind, model, ahaEnabled);
      if (protocol === "wss") await connectWss(endpoint);
      else await connectWebRtc(endpoint);
      setConnection("connected");
      setNotice(`${protocol === "wss" ? "WSS" : "WebRTC"} 已连接，请开始说话`);
    } catch (reason) {
      await stop();
      setConnection("error");
      setNotice(errorMessage(reason, "实时测试连接失败。"));
    }
  };

  const connectionLocked = connection === "connecting" || connection === "connected";
  const transcriptBadge = transcriptView.phase === "listening"
    ? "聆听中"
    : transcriptView.phase === "interim"
      ? "中间结果"
      : transcriptView.phase === "processing" ? "处理中" : null;
  const transcriptIsPending = ["listening", "interim", "processing"].includes(transcriptView.phase);
  const backendVisible = backendActivity.sessionId !== null || backendActivity.status !== "idle" || backendApproval !== null || backendQuestion !== null;
  const backendStatusLabel = ({ idle: "空闲", running: "执行中", summarizing: "正在总结", failed: "失败", cancelled: "已取消" } as const)[backendActivity.status];

  return (
    <div className="test-console">
      <section className="panel test-control-panel">
        <div className="panel-heading"><div><h2>{workflowKind === "voice_agent" ? "端到端对话测试" : "实时转写测试"}</h2><p>{workflowKind === "voice_agent" ? "音频、转写和回复仅保留在当前浏览器内存。" : "验证 VAD、切段与 ASR 转写；不会调用 Responses 或 TTS。"}</p></div><span className={`connection-indicator connection-${connection}`}>{connection === "connected" ? "已连接" : connection === "connecting" ? "连接中" : connection === "error" ? "连接失败" : "未连接"}</span></div>
        <div className="test-connection-options"><div className="protocol-tabs"><button className={protocol === "wss" ? "active" : ""} disabled={connectionLocked} onClick={() => setProtocol("wss")}>WSS</button><button className={protocol === "webrtc" ? "active" : ""} disabled={connectionLocked} onClick={() => setProtocol("webrtc")}>WebRTC</button></div><label className="switch-control aha-extension-toggle"><input type="checkbox" checked={ahaEnabled} disabled={connectionLocked} onChange={(event) => setAhaEnabled(event.target.checked)} /><span>启用 AHA 扩展协议</span></label></div>
        <div className="test-status"><span className="pulse-dot" /><strong>{notice}</strong></div>
        {!canTest && <div className="alert alert-warning">{blockedReason ?? "请先完成配置、发布资源并创建公开路由。"}</div>}
        <div className="panel-actions"><button className="primary-button" disabled={!canTest || connection === "connecting" || connection === "connected"} title={!canTest ? blockedReason : undefined} onClick={() => void start()}>开始麦克风测试</button><button className="secondary-button" disabled={connection === "idle"} onClick={() => void stop()}>停止并清除</button></div>
        <audio ref={remoteAudioRef} autoPlay className="remote-audio" />
      </section>

      <section className={`conversation-grid ${workflowKind === "transcription" ? "transcription-only" : ""}`}>
        <article className="panel conversation-card"><div className="conversation-label">用户转写{transcriptBadge && <span className="interim-badge">{transcriptBadge}</span>}</div><p className={transcriptIsPending ? "interim-transcript" : undefined}>{transcriptView.text || "开始说话后，实时转写会显示在这里。"}</p></article>
        {workflowKind === "voice_agent" && <article className="panel conversation-card assistant-card"><div className="conversation-label">模型回复</div><p>{assistant || "Responses 文本增量会显示在这里，并同步送入 TTS。"}</p></article>}
      </section>

      {ahaEnabled && workflowKind === "voice_agent" && backendVisible && <section className="panel backend-runtime-panel" aria-label="Agent Backend 执行状态">
        <details className="backend-runtime-details"><summary><div><p className="eyebrow">AGENT BACKEND</p><strong>DSH 执行进度</strong><small>{backendActivity.sessionId ? `Session ${backendActivity.sessionId}` : "正在建立 DSH session"}</small></div><span className={`status-chip backend-status-${backendActivity.status}`}>{backendStatusLabel}</span><i aria-hidden="true">⌄</i></summary>
          <div className="backend-runtime-content">
            {backendActivity.status === "running" && <div className="panel-actions"><button className="danger-button" onClick={cancelBackend}>取消 DSH 任务</button></div>}
            {backendActivity.text && <div className="backend-progress-text">{backendActivity.text}</div>}
            {backendActivity.tools.length > 0 && <div className="backend-tool-list">{backendActivity.tools.map((tool) => <article key={tool.callId}><span className={`backend-tool-state backend-tool-${tool.status}`}>{tool.status === "running" ? "运行中" : tool.status === "completed" ? "完成" : "失败"}</span><strong>{tool.name}</strong><code>{tool.detail || "等待结果"}</code></article>)}</div>}
            {backendActivity.message && <div className={backendActivity.status === "failed" ? "alert alert-error" : "alert alert-warning"}>{backendActivity.message}</div>}
          </div>
        </details>
      </section>}

      {ahaEnabled && backendApproval && <section className="panel backend-interaction-card backend-approval-card" aria-label="DSH 审批请求"><div><p className="eyebrow">APPROVAL REQUIRED</p><h2>{backendApproval.toolName} 请求执行</h2><p>{backendApproval.reason || "DSH 未提供额外审批原因。"}</p></div><div className="panel-actions"><button className="danger-button" disabled={backendApproval.submitting} onClick={() => answerApproval(false)}>拒绝</button><button className="primary-button" disabled={backendApproval.submitting} onClick={() => answerApproval(true)}>{backendApproval.submitting ? "正在提交…" : "仅允许这次"}</button></div></section>}

      {ahaEnabled && backendQuestion && <section className="panel backend-interaction-card backend-question-card" aria-label="DSH 结构化问题"><div className="panel-heading"><div><p className="eyebrow">INPUT REQUIRED</p><h2>DSH 需要补充信息</h2><p>界面回答与下一句语音回答先到者生效。</p></div></div><div className="backend-question-list">{backendQuestion.questions.map((question) => <fieldset key={question.id} disabled={backendQuestion.submitting}><legend>{question.question || question.id}</legend>{question.options.map((option) => { const selected = (backendQuestion.selections[question.id] ?? []).includes(option.label); return <label className="backend-question-option" key={option.label}><input type={question.multiSelect ? "checkbox" : "radio"} name={`backend-question-${question.id}`} checked={selected} onChange={() => toggleQuestionOption(question, option.label)} /><span><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</span></label>; })}<label><span>自定义回答（可选）</span><input value={backendQuestion.custom[question.id] ?? ""} onChange={(event) => setBackendQuestion({ ...backendQuestion, custom: { ...backendQuestion.custom, [question.id]: event.target.value } })} /></label></fieldset>)}</div><div className="panel-actions"><button className="primary-button" disabled={backendQuestion.submitting} onClick={answerQuestions}>{backendQuestion.submitting ? "正在提交…" : "提交结构化答案"}</button></div></section>}

      <section className={`latency-grid ${workflowKind === "transcription" ? "transcription-only" : ""}`}>
        <div className="metric-card"><span>检测到语音 → 首个中间结果</span><strong>{displayMs(latencies.interim, "等待中间结果")}</strong></div>
        <div className="metric-card"><span>话音结束 → 最终转写</span><strong>{displayMs(latencies.transcription, "等待最终转写")}</strong></div>
        {workflowKind === "voice_agent" && <><div className="metric-card"><span>转写 → 模型首字</span><strong>{displayMs(latencies.model, "等待模型首字")}</strong></div><div className="metric-card"><span>模型首字 → TTS 首个音频块</span><strong>{displayMs(latencies.audio, "等待 TTS 音频")}</strong></div></>}
      </section>

      {ahaEnabled && <section className="latency-grid transcription-only" aria-label="AHA 处理状态">
        <div className="metric-card"><span>VAD 处理位置</span><strong>{vadProgress ? `${vadProgress.final ? "100% · " : "处理中 · "}${displayMs(vadProgress.processedAudioMs, "等待进度")}` : "等待进度"}</strong></div>
        <div className="metric-card"><span>最终聚类状态</span><strong>{finalCluster ? finalCluster.status === "completed" ? `完成 r${finalCluster.revision} · ${finalCluster.assignments} 个片段` : `失败 r${finalCluster.revision}` : "等待最终快照"}</strong></div>
      </section>}

      {(vectorSearch || speakerFilter) && <div className="realtime-diagnostics-grid">
        {vectorSearch && <details className="panel realtime-vector-results" aria-label="人物搜索结果" open={vectorResultsOpen} onToggle={(event) => setVectorResultsOpen(event.currentTarget.open)}>
          <summary><span>人物搜索结果</span><small>{vectorSearch.error ? "检索失败" : `${vectorSearch.groups.length} 个候选人`}</small><i aria-hidden="true">⌄</i></summary>
          <div className="realtime-vector-content">{vectorSearch.error ? <div className="inline-error">{vectorSearch.error}</div> : vectorSearch.groups.length > 0 ? <div className="realtime-vector-matches">{vectorSearch.groups.map((group, index) => <div key={`${group.speakerId}-${index}`}><code>{group.speakerId || "未提供 Speaker ID"}</code><b>分数 {group.score.toFixed(4)}</b></div>)}</div> : <p className="empty-copy">本次搜索没有候选人。</p>}</div>
        </details>}
        {speakerFilter && <section className="panel realtime-speaker-filter" aria-label="说话人过滤结果">
          <header><span>说话人过滤</span><b className={`filter-status filter-status-${speakerFilter.status}`}>{speakerFilter.status === "matched" ? "已放行" : speakerFilter.status === "rejected" ? "已拒绝" : "过滤失败"}</b></header>
          <div className="realtime-filter-result">
            {speakerFilter.status === "matched" ? <><code>{speakerFilter.speakerId ?? "未提供 Speaker ID"}</code><strong>分数 {speakerFilter.score?.toFixed(4) ?? "—"}</strong></> : <strong>{speakerFilter.reason ?? speakerFilter.error}</strong>}
            <small>音频 {speakerFilter.audioStartMs ?? "—"}–{speakerFilter.audioEndMs ?? "—"} ms</small>
          </div>
        </section>}
      </div>}

      {streamSummaries.length > 0 && <section className="panel stream-event-summaries" aria-label="流式事件摘要">
        <details><summary><span>流式事件摘要</span><small>高频事件已汇总 · {streamSummaries.length} 项</small><i aria-hidden="true">⌄</i></summary>
        <div>{streamSummaries.map((summary) => <article key={summary.key}>
          <code>{summary.type}</code>
          <b className={`stream-event-status stream-event-status-${summary.status}`}>{summary.status === "completed" ? "完成" : "流式中"}</b>
          <small>{summary.time} · ×{summary.count}</small>
          <span>{summary.detail}</span>
        </article>)}</div></details>
      </section>}

      <details className="panel event-log"><summary>协议事件 <span>{eventTotal}</span></summary><div>{events.map((event) => <div className="event-row" key={event.id}><time>{event.time}</time><code>{event.type}</code>{event.count > 1 && <b>×{event.count}</b>}<span>{event.detail}</span></div>)}</div></details>
    </div>
  );
}
