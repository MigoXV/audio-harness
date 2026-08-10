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
  transcription?: number;
  model?: number;
  audio?: number;
}

export function eventDetail(event: Record<string, unknown>): string {
  if (event.type === "response.output_audio.delta" || event.type === "audio_harness.tts_first_audio") {
    return "音频数据已省略";
  }
  if (event.type === "x_aha.vad.physical_speech_stopped") {
    return `物理停顿 ${String(event.audio_start_ms)}–${String(event.audio_end_ms)} ms；语义轮次未结束`;
  }
  return String(
    event.delta
      ?? event.transcript
      ?? (event.error as { message?: string } | undefined)?.message
      ?? "",
  );
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

export function realtimeEndpoint(protocol: "wss" | "webrtc", model: string, ahaEnabled: boolean): string {
  const extension = ahaEnabled ? "&x_aha=v1" : "";
  return protocol === "wss"
    ? `/v1/realtime?intent=transcription${extension}`
    : `/v1/realtime/calls?model=${encodeURIComponent(model)}${extension}`;
}

export function isResponseTextDelta(type: string): boolean {
  return type === "response.output_text.delta"
    || type === "response.output_audio_transcript.delta";
}

export function isTtsFirstAudio(type: string): boolean {
  return type === "audio_harness.tts_first_audio" || type === "response.output_audio.delta";
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
  const [protocol, setProtocol] = useState<"wss" | "webrtc">("webrtc");
  const [ahaEnabled, setAhaEnabled] = useState(true);
  const [connection, setConnection] = useState<"idle" | "connecting" | "connected" | "error">("idle");
  const [notice, setNotice] = useState("等待开始测试");
  const [transcript, setTranscript] = useState("");
  const [assistant, setAssistant] = useState("");
  const [events, setEvents] = useState<EventLine[]>([]);
  const [eventTotal, setEventTotal] = useState(0);
  const [latencies, setLatencies] = useState<Latencies>({});
  const socketRef = useRef<WebSocket | null>(null);
  const microphoneRef = useRef<MicrophoneCapture | null>(null);
  const peerRef = useRef<RTCPeerConnection | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const remoteAudioRef = useRef<HTMLAudioElement | null>(null);
  const playerRef = useRef(new PcmPlayer());
  const stoppingRef = useRef(false);
  const speechStoppedAt = useRef<number | null>(null);
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
    if (type === "input_audio_buffer.speech_started") {
      speechStoppedAt.current = null;
      transcriptAt.current = null;
      modelAt.current = null;
      setNotice("检测到用户语音；旧回复已中断");
      playerRef.current.clear();
      setTranscript("");
      setAssistant("");
      setLatencies({});
    } else if (type === "x_aha.vad.physical_speech_stopped") {
      setNotice("检测到物理停顿，语义 VAD 仍在等待本轮后续语音");
    } else if (type === "input_audio_buffer.speech_stopped") {
      speechStoppedAt.current = performance.now();
      setNotice("话音结束，正在转写");
    } else if (type === "conversation.item.input_audio_transcription.delta") {
      setTranscript((current) => current + String(event.delta ?? ""));
    } else if (type === "conversation.item.input_audio_transcription.completed") {
      const now = performance.now();
      transcriptAt.current = now;
      setTranscript(String(event.transcript ?? ""));
      if (speechStoppedAt.current !== null) setLatencies((current) => ({ ...current, transcription: now - speechStoppedAt.current! }));
      setNotice(workflowKind === "voice_agent" ? "转写完成，大模型正在回复" : "转写完成，可以继续说话");
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
      socket.onerror = () => reject(new Error("WSS 握手失败，请检查 TLS 和工作流状态"));
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
    setNotice("正在检查工作流、路由和麦克风");
    setEvents([]);
    setEventTotal(0);
    setTranscript("");
    setAssistant("");
    setLatencies({});
    speechStoppedAt.current = null;
    transcriptAt.current = null;
    modelAt.current = null;
    try {
      const route = await api.getRealtimeRoute(model);
      if (route.visibility !== "public") throw new Error("当前 Realtime model 不是公开路由");
      const endpoint = realtimeEndpoint(protocol, model, ahaEnabled);
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

  return (
    <div className="test-console">
      <section className="panel test-control-panel">
        <div className="panel-heading"><div><h2>{workflowKind === "voice_agent" ? "端到端对话测试" : "实时转写测试"}</h2><p>{workflowKind === "voice_agent" ? "音频、转写和回复仅保留在当前浏览器内存。" : "验证 VAD、切段与 ASR 转写；不会调用 Responses 或 TTS。"}</p></div><span className={`connection-indicator connection-${connection}`}>{connection === "connected" ? "已连接" : connection === "connecting" ? "连接中" : connection === "error" ? "连接失败" : "未连接"}</span></div>
        <div className="test-connection-options"><div className="protocol-tabs"><button className={protocol === "webrtc" ? "active" : ""} disabled={connection === "connected"} onClick={() => setProtocol("webrtc")}>WebRTC</button><button className={protocol === "wss" ? "active" : ""} disabled={connection === "connected" || workflowKind === "voice_agent"} onClick={() => setProtocol("wss")}>WSS</button></div><label className="switch-control aha-extension-toggle"><input type="checkbox" checked={ahaEnabled} disabled={connection !== "idle" && connection !== "error"} onChange={(event) => setAhaEnabled(event.target.checked)} /><span>启用 AHA 扩展协议</span></label></div>
        <div className="test-status"><span className="pulse-dot" /><strong>{notice}</strong></div>
        {!canTest && <div className="alert alert-warning">{blockedReason ?? "请先完成配置并发布工作流。"}</div>}
        <div className="panel-actions"><button className="primary-button" disabled={!canTest || connection === "connecting" || connection === "connected"} title={!canTest ? blockedReason : undefined} onClick={() => void start()}>开始麦克风测试</button><button className="secondary-button" disabled={connection === "idle"} onClick={() => void stop()}>停止并清除</button></div>
        <audio ref={remoteAudioRef} autoPlay className="remote-audio" />
      </section>

      <section className={`conversation-grid ${workflowKind === "transcription" ? "transcription-only" : ""}`}>
        <article className="panel conversation-card"><div className="conversation-label">用户转写</div><p>{transcript || "开始说话后，实时转写会显示在这里。"}</p></article>
        {workflowKind === "voice_agent" && <article className="panel conversation-card assistant-card"><div className="conversation-label">模型回复</div><p>{assistant || "Responses 文本增量会显示在这里，并同步送入 TTS。"}</p></article>}
      </section>

      <section className={`latency-grid ${workflowKind === "transcription" ? "transcription-only" : ""}`}>
        <div className="metric-card"><span>话音结束 → 最终转写</span><strong>{displayMs(latencies.transcription, "等待最终转写")}</strong></div>
        {workflowKind === "voice_agent" && <><div className="metric-card"><span>转写 → 模型首字</span><strong>{displayMs(latencies.model, "等待模型首字")}</strong></div><div className="metric-card"><span>模型首字 → TTS 首个音频块</span><strong>{displayMs(latencies.audio, "等待 TTS 音频")}</strong></div></>}
      </section>

      <details className="panel event-log"><summary>协议事件 <span>{eventTotal}</span></summary><div>{events.map((event) => <div className="event-row" key={event.id}><time>{event.time}</time><code>{event.type}</code>{event.count > 1 && <b>×{event.count}</b>}<span>{event.detail}</span></div>)}</div></details>
    </div>
  );
}
