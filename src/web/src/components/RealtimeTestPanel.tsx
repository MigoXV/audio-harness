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
  if (event.type === "x_aha.vad.physical_speech_stopped") {
    return `物理停顿 ${String(event.audio_start_ms)}–${String(event.audio_end_ms)} ms；语义轮次未结束`;
  }
  if (event.type === "x_aha.input_audio_transcription.interim") {
    return `预览 r${String(event.revision)} · ${String(event.audio_start_ms)}–${String(event.audio_end_ms)} ms · ${String(event.transcript ?? "")}`;
  }
  if (event.type === "x_aha.input_audio_transcription.interim_cleared") {
    return `中间结果已清除：${String(event.reason ?? "unknown")}`;
  }
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
  const [vectorSearch, setVectorSearch] = useState<VectorSearchEventView | null>(null);
  const [vectorResultsOpen, setVectorResultsOpen] = useState(true);
  const [speakerFilter, setSpeakerFilter] = useState<SpeakerFilterEventView | null>(null);
  const [vadProgress, setVadProgress] = useState<VadProgressView | null>(null);
  const [finalCluster, setFinalCluster] = useState<FinalClusterView | null>(null);
  const [latencies, setLatencies] = useState<Latencies>({});
  const socketRef = useRef<WebSocket | null>(null);
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

  const appendEvent = (event: Record<string, unknown>) => {
    const detail = eventDetail(event);
    setEventTotal((current) => current + 1);
    setEvents((current) => appendGroupedEvent(current, {
      id: String(event.event_id ?? `${event.type}-${performance.now()}`),
      type: String(event.type ?? "unknown"),
      time: new Date().toLocaleTimeString("zh-CN", { hour12: false }),
      detail,
      count: 1,
    }));
  };

  const handleServerEvent = (event: Record<string, unknown>) => {
    appendEvent(event);
    const type = String(event.type ?? "");
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
    } else if (type === "x_aha.vad.physical_speech_stopped") {
      setNotice("检测到物理停顿，语义 VAD 仍在等待本轮后续语音");
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
      setNotice("回复完成，可以继续说话");
    } else if (type === "error") {
      setNotice(`运行错误：${String((event.error as { message?: string } | undefined)?.message ?? "未知错误")}`);
    }
  };

  const stop = async () => {
    stoppingRef.current = true;
    socketRef.current?.close();
    socketRef.current = null;
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
    setVectorSearch(null);
    setVectorResultsOpen(true);
    setSpeakerFilter(null);
    setVadProgress(null);
    setFinalCluster(null);
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

      <details className="panel event-log"><summary>协议事件 <span>{eventTotal}</span></summary><div>{events.map((event) => <div className="event-row" key={event.id}><time>{event.time}</time><code>{event.type}</code>{event.count > 1 && <b>×{event.count}</b>}<span>{event.detail}</span></div>)}</div></details>
    </div>
  );
}
