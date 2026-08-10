import { useEffect, useRef, useState } from "react";
import type {
  RealtimeClientEvent,
  RealtimeServerEvent,
} from "openai/resources/realtime/realtime";

import {
  decodeAudioToPcm,
  lastVoicedOffsetMilliseconds,
  pcm16ToBase64,
  silenceBase64,
  splitPcmChunks,
  startMicrophone,
  type MicrophoneCapture,
} from "./audio";

interface PublicConfig {
  base_url: string;
  model: string;
  connection_mode: "intent" | "model";
}

interface TranscriptLine {
  itemId: string;
  text: string;
  final: boolean;
  endToFirstCharacterMs?: number;
  endToCompletionMs?: number;
  vadConfirmationLatencyMs?: number;
  asrFirstCharacterLatencyMs?: number;
  asrCompletionLatencyMs?: number;
  completedAtLabel?: string;
  endpointEstimatedLocally?: boolean;
  charactersPerSecond?: number;
}

interface EventLine {
  id: string;
  type: string;
  detail: string;
  time: string;
}

type ConnectionState =
  | "disconnected"
  | "connecting"
  | "ready"
  | "error";
type StreamMode = "idle" | "microphone" | "file";

interface TranscriptTiming {
  speechEndAt: number;
  vadReferenceEndAt: number;
  vadConfirmedAt: number;
  endpointEstimatedLocally: boolean;
  firstCharacterAt?: number;
  firstCharacterPaintedAt?: number;
  paintScheduled?: boolean;
}

interface AudioTimeline {
  startedAt: number | null;
  lastVoicedAt: number | null;
  accumulatedAudioMs: number;
}

type ServerEvent = RealtimeServerEvent;

const PCM_SAMPLE_RATE = 24_000;

const sleep = (milliseconds: number) =>
  new Promise((resolve) => window.setTimeout(resolve, milliseconds));

function audioDurationMilliseconds(audio: string): number {
  const padding = audio.endsWith("==") ? 2 : audio.endsWith("=") ? 1 : 0;
  const byteLength = (audio.length * 3) / 4 - padding;
  return (byteLength / 2 / PCM_SAMPLE_RATE) * 1_000;
}

function characterCount(text: string): number {
  return Array.from(text).filter((character) => !/\s/u.test(character))
    .length;
}

function formatLatency(milliseconds: number): string {
  return milliseconds < 1_000
    ? `${Math.round(milliseconds)} ms`
    : `${(milliseconds / 1_000).toFixed(2)} s`;
}

function eventDetail(event: ServerEvent): string {
  switch (event.type) {
    case "input_audio_buffer.speech_started":
      return `语音起点 ${event.audio_start_ms} ms`;
    case "input_audio_buffer.speech_stopped":
      return `语音终点 ${event.audio_end_ms} ms`;
    case "conversation.item.input_audio_transcription.completed":
      return event.transcript || "空转写";
    case "conversation.item.input_audio_transcription.delta":
      return event.delta || "增量";
    case "error":
      return event.error.message;
    default:
      return "";
  }
}

function App() {
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [connection, setConnection] =
    useState<ConnectionState>("disconnected");
  const [streamMode, setStreamMode] = useState<StreamMode>("idle");
  const [transcripts, setTranscripts] = useState<TranscriptLine[]>([]);
  const [events, setEvents] = useState<EventLine[]>([]);
  const [notice, setNotice] = useState("等待连接");
  const socketRef = useRef<WebSocket | null>(null);
  const connectPromiseRef = useRef<Promise<WebSocket> | null>(null);
  const microphoneRef = useRef<MicrophoneCapture | null>(null);
  const microphoneStartingRef = useRef(false);
  const [microphoneStarting, setMicrophoneStarting] = useState(false);
  const cancelFileRef = useRef(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const transcriptTimingsRef = useRef(new Map<string, TranscriptTiming>());
  const audioTimelineRef = useRef<AudioTimeline>({
    startedAt: null,
    lastVoicedAt: null,
    accumulatedAudioMs: 0,
  });
  const [eventsExpanded, setEventsExpanded] = useState(false);

  useEffect(() => {
    fetch("/api/config")
      .then(async (response) => {
        if (!response.ok) {
          throw new Error("无法读取 Demo 配置");
        }
        return (await response.json()) as PublicConfig;
      })
      .then(setConfig)
      .catch((error: Error) => {
        setNotice(error.message);
        setConnection("error");
      });

    return () => {
      socketRef.current?.close();
      microphoneRef.current?.stop().catch(() => undefined);
    };
  }, []);

  const appendEvent = (event: ServerEvent) => {
    setEvents((current) =>
      [
        {
          id:
            ("event_id" in event && event.event_id) ||
            `${event.type}-${Date.now()}`,
          type: event.type,
          detail: eventDetail(event),
          time: new Date().toLocaleTimeString("zh-CN", {
            hour12: false,
          }),
        },
        ...current,
      ].slice(0, 60),
    );
  };

  const updateTranscript = (
    itemId: string,
    text: string,
    final: boolean,
    completedAt = performance.now(),
  ) => {
    const timing = transcriptTimingsRef.current.get(itemId);
    let capturedFirstCharacter = false;
    if (
      text.length > 0 &&
      timing !== undefined &&
      timing.firstCharacterAt === undefined
    ) {
      timing.firstCharacterAt = completedAt;
      capturedFirstCharacter = true;
    }
    const firstCharacterVisibleAt =
      timing?.firstCharacterPaintedAt ?? timing?.firstCharacterAt;
    const endToFirstCharacterMs =
      timing === undefined || firstCharacterVisibleAt === undefined
        ? undefined
        : Math.max(0, firstCharacterVisibleAt - timing.speechEndAt);
    const endToCompletionMs =
      !final || timing === undefined
        ? undefined
        : Math.max(0, completedAt - timing.speechEndAt);
    const vadConfirmationLatencyMs =
      timing === undefined
        ? undefined
        : Math.max(0, timing.vadConfirmedAt - timing.vadReferenceEndAt);
    const asrFirstCharacterLatencyMs =
      timing?.firstCharacterAt === undefined
        ? undefined
        : Math.max(
            0,
            timing.firstCharacterAt - timing.vadConfirmedAt,
          );
    const asrCompletionLatencyMs =
      !final || timing === undefined
        ? undefined
        : Math.max(0, completedAt - timing.vadConfirmedAt);
    const elapsedSeconds =
      timing === undefined
        ? 0
        : Math.max(0, completedAt - timing.vadConfirmedAt) / 1_000;
    const charactersPerSecond =
      final && elapsedSeconds > 0
        ? characterCount(text) / elapsedSeconds
        : undefined;

    setTranscripts((current) => {
      const existing = current.find((line) => line.itemId === itemId);
      if (existing === undefined) {
        return [
          ...current,
          {
            itemId,
            text,
            final,
            endToFirstCharacterMs,
            endToCompletionMs,
            vadConfirmationLatencyMs,
            asrFirstCharacterLatencyMs,
            asrCompletionLatencyMs,
            completedAtLabel: final
              ? new Date().toLocaleTimeString("zh-CN", {
                  hour12: false,
                })
              : undefined,
            endpointEstimatedLocally:
              timing?.endpointEstimatedLocally,
            charactersPerSecond,
          },
        ];
      }
      return current.map((line) =>
        line.itemId === itemId
          ? {
              ...line,
              text: final ? text : `${line.text}${text}`,
              final,
              endToFirstCharacterMs:
                endToFirstCharacterMs ??
                line.endToFirstCharacterMs,
              endToCompletionMs:
                endToCompletionMs ?? line.endToCompletionMs,
              vadConfirmationLatencyMs:
                vadConfirmationLatencyMs ??
                line.vadConfirmationLatencyMs,
              asrFirstCharacterLatencyMs:
                asrFirstCharacterLatencyMs ??
                line.asrFirstCharacterLatencyMs,
              asrCompletionLatencyMs:
                asrCompletionLatencyMs ?? line.asrCompletionLatencyMs,
              completedAtLabel:
                final
                  ? new Date().toLocaleTimeString("zh-CN", {
                      hour12: false,
                    })
                  : line.completedAtLabel,
              endpointEstimatedLocally:
                timing?.endpointEstimatedLocally ??
                line.endpointEstimatedLocally,
              charactersPerSecond:
                charactersPerSecond ?? line.charactersPerSecond,
            }
          : line,
      );
    });

    if (
      capturedFirstCharacter &&
      timing !== undefined &&
      !timing.paintScheduled
    ) {
      timing.paintScheduled = true;
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
          const firstCharacterPaintedAt = performance.now();
          timing.firstCharacterPaintedAt = firstCharacterPaintedAt;
          setTranscripts((current) =>
            current.map((line) =>
              line.itemId === itemId
                ? {
                    ...line,
                    endToFirstCharacterMs: Math.max(
                      0,
                      firstCharacterPaintedAt - timing.speechEndAt,
                    ),
                  }
                : line,
            ),
          );
        });
      });
    }
  };

  const handleServerEvent = (event: ServerEvent) => {
    appendEvent(event);
    switch (event.type) {
      case "conversation.item.input_audio_transcription.delta":
        updateTranscript(
          event.item_id,
          event.delta || "",
          false,
          performance.now(),
        );
        break;
      case "conversation.item.input_audio_transcription.completed":
        updateTranscript(
          event.item_id,
          event.transcript,
          true,
          performance.now(),
        );
        setNotice("片段转写完成");
        break;
      case "conversation.item.input_audio_transcription.failed":
        setNotice(`转写失败：${event.error.message}`);
        break;
      case "input_audio_buffer.speech_started":
        setNotice("检测到语音");
        break;
      case "input_audio_buffer.speech_stopped": {
        const vadConfirmedAt = performance.now();
        const localSpeechEndAt =
          audioTimelineRef.current.lastVoicedAt;
        const serverSpeechEndAt =
          audioTimelineRef.current.startedAt === null
            ? vadConfirmedAt
            : audioTimelineRef.current.startedAt + event.audio_end_ms;
        audioTimelineRef.current.lastVoicedAt = null;
        transcriptTimingsRef.current.set(event.item_id, {
          speechEndAt: serverSpeechEndAt,
          vadReferenceEndAt: localSpeechEndAt ?? serverSpeechEndAt,
          vadConfirmedAt,
          endpointEstimatedLocally: localSpeechEndAt !== null,
        });
        setNotice("语音片段已提交，正在识别");
        break;
      }
      case "error":
        setNotice(`Realtime 错误：${event.error.message}`);
        break;
    }
  };

  const sessionUpdate = (): RealtimeClientEvent => ({
    type: "session.update",
    session: {
      type: "transcription",
      audio: {
        input: {
          format: {
            type: "audio/pcm",
            rate: 24_000,
          },
          transcription: {
            model: config?.model || "gpt-4o-transcribe",
            language: "zh",
          },
          turn_detection: {
            type: "server_vad",
            create_response: false,
          },
        },
      },
    },
  });

  const connect = (): Promise<WebSocket> => {
    if (
      socketRef.current?.readyState === WebSocket.OPEN &&
      connection === "ready"
    ) {
      return Promise.resolve(socketRef.current);
    }
    if (connectPromiseRef.current !== null) {
      return connectPromiseRef.current;
    }

    setConnection("connecting");
    setNotice("正在建立 Realtime 会话");
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
    socketRef.current = socket;
    transcriptTimingsRef.current.clear();
    audioTimelineRef.current = {
      startedAt: null,
      lastVoicedAt: null,
      accumulatedAudioMs: 0,
    };

    const pending = new Promise<WebSocket>((resolve, reject) => {
      let settled = false;
      socket.onmessage = (message) => {
        const event = JSON.parse(String(message.data)) as ServerEvent;
        handleServerEvent(event);
        if (event.type === "session.created") {
          socket.send(JSON.stringify(sessionUpdate()));
        } else if (event.type === "session.updated" && !settled) {
          settled = true;
          setConnection("ready");
          setNotice("Realtime 会话已就绪");
          resolve(socket);
        } else if (event.type === "error" && !settled) {
          settled = true;
          setConnection("error");
          reject(new Error(event.error.message));
        }
      };
      socket.onerror = () => {
        if (!settled) {
          settled = true;
          reject(new Error("WebSocket 连接失败"));
        }
        setConnection("error");
        setNotice("WebSocket 连接失败");
      };
      socket.onclose = () => {
        socketRef.current = null;
        connectPromiseRef.current = null;
        setConnection("disconnected");
        setNotice("Realtime 会话已断开");
        if (!settled) {
          settled = true;
          reject(new Error("Realtime 会话在就绪前断开"));
        }
      };
    });
    connectPromiseRef.current = pending;
    return pending;
  };

  const sendClientEvent = (
    socket: WebSocket,
    event: RealtimeClientEvent,
  ) => {
    if (socket.readyState !== WebSocket.OPEN) {
      throw new Error("Realtime 会话尚未连接");
    }
    socket.send(JSON.stringify(event));
  };

  const sendAudio = (socket: WebSocket, audio: string) => {
    const duration = audioDurationMilliseconds(audio);
    const now = performance.now();
    const timeline = audioTimelineRef.current;
    if (timeline.startedAt === null) {
      timeline.startedAt = now - duration;
    }
    const chunkStartMs = timeline.accumulatedAudioMs;
    timeline.accumulatedAudioMs += duration;
    const lastVoicedOffset = lastVoicedOffsetMilliseconds(audio);
    if (lastVoicedOffset !== undefined) {
      timeline.lastVoicedAt =
        timeline.startedAt + chunkStartMs + lastVoicedOffset;
    }
    sendClientEvent(socket, {
      type: "input_audio_buffer.append",
      audio,
    });
  };

  const sendTrailingSilence = async (socket: WebSocket) => {
    for (const chunk of silenceBase64()) {
      sendAudio(socket, chunk);
      await sleep(100);
    }
  };

  const stopCurrentStream = async () => {
    cancelFileRef.current = true;
    const microphone = microphoneRef.current;
    microphoneRef.current = null;
    if (microphone !== null) {
      await microphone.stop();
      const socket = socketRef.current;
      if (socket?.readyState === WebSocket.OPEN) {
        await sendTrailingSilence(socket);
      }
    }
    setStreamMode("idle");
    setNotice("输入已停止，等待最后一个片段");
  };

  const startMic = async () => {
    if (microphoneStartingRef.current || microphoneRef.current !== null) {
      return;
    }
    microphoneStartingRef.current = true;
    setMicrophoneStarting(true);
    try {
      const socket = await connect();
      const microphone = await startMicrophone((audio) => {
        sendAudio(socket, audio);
      });
      microphoneRef.current = microphone;
      setStreamMode("microphone");
      setNotice("正在收听麦克风");
    } catch (error) {
      setNotice(
        error instanceof Error ? error.message : "无法启动麦克风",
      );
    } finally {
      microphoneStartingRef.current = false;
      setMicrophoneStarting(false);
    }
  };

  const streamFile = async (file: Blob) => {
    try {
      cancelFileRef.current = false;
      setStreamMode("file");
      setNotice("正在解码音频");
      const [socket, audio] = await Promise.all([
        connect(),
        file.arrayBuffer().then(decodeAudioToPcm),
      ]);
      setNotice("正在按实时速度发送音频");
      for (const chunk of splitPcmChunks(audio)) {
        if (cancelFileRef.current) {
          break;
        }
        sendAudio(socket, pcm16ToBase64(chunk));
        await sleep((chunk.length / 24_000) * 1_000);
      }
      await sendTrailingSilence(socket);
      setNotice("音频发送完成，等待转写");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "音频发送失败");
    } finally {
      setStreamMode("idle");
    }
  };

  const useSample = async () => {
    try {
      const response = await fetch("/api/sample");
      if (!response.ok) {
        throw new Error(await response.text());
      }
      await streamFile(await response.blob());
    } catch (error) {
      setNotice(
        error instanceof Error ? error.message : "无法读取测试音频",
      );
    }
  };

  const disconnect = async () => {
    await stopCurrentStream();
    socketRef.current?.close(1000, "用户断开");
  };

  const clearTranscript = () => {
    setTranscripts([]);
    setEvents([]);
    transcriptTimingsRef.current.clear();
    audioTimelineRef.current = {
      startedAt: null,
      lastVoicedAt: null,
      accumulatedAudioMs: 0,
    };
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) {
      sendClientEvent(socket, {
        type: "input_audio_buffer.clear",
      });
    }
  };

  const busy = streamMode !== "idle";

  return (
    <main className="desk">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">AH</span>
          <div>
            <strong>Realtime Transcription Desk</strong>
            <span>流式 ASR + VAD 验证台</span>
          </div>
        </div>
        <div className="target">
          <span>{config?.base_url || "读取配置中"}</span>
          <strong>{config?.model || "—"}</strong>
        </div>
        <div className={`connection connection-${connection}`}>
          <i />
          {connection === "ready"
            ? "已连接"
            : connection === "connecting"
              ? "连接中"
              : connection === "error"
                ? "异常"
                : "未连接"}
        </div>
      </header>

      <section
        className={`workspace ${
          eventsExpanded ? "events-open" : "events-collapsed"
        }`}
      >
        <article className="transcript-pane">
          <div className="section-heading">
            <div>
              <span className="eyebrow">LIVE TRANSCRIPT</span>
              <h1>实时转写</h1>
            </div>
            <button className="text-button" onClick={clearTranscript}>
              清空
            </button>
          </div>
          <div className="transcript-list" aria-live="polite">
            {transcripts.length === 0 ? (
              <div className="empty-state">
                <span>01</span>
                <p>打开麦克风，或发送一段 WAV 音频。</p>
                <small>VAD 确认完整语音片段后生成转写。</small>
              </div>
            ) : (
              transcripts.map((line, index) => (
                <div
                  className={`transcript-line ${
                    line.final ? "is-final" : "is-draft"
                  }`}
                  key={line.itemId}
                >
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <p>{line.text || "…"}</p>
                  <div className="transcript-metrics">
                    <div className="transcript-status">
                      <small>{line.final ? "FINAL" : "STREAMING"}</small>
                      {line.completedAtLabel !== undefined && (
                        <time>完成于 {line.completedAtLabel}</time>
                      )}
                    </div>
                    <div className="metric-grid">
                      {line.endToFirstCharacterMs !== undefined && (
                        <div
                          className="metric"
                          title={
                            "服务端音频终点到首个转写字符完成绘制的耗时"
                          }
                        >
                          <span>端到端首字</span>
                          <strong>
                            {formatLatency(line.endToFirstCharacterMs)}
                          </strong>
                        </div>
                      )}
                      {line.endToCompletionMs !== undefined && (
                        <div
                          className="metric"
                          title="服务端音频终点到最终转写完成的耗时"
                        >
                          <span>句末到完成</span>
                          <strong>
                            {formatLatency(line.endToCompletionMs)}
                          </strong>
                        </div>
                      )}
                      {line.vadConfirmationLatencyMs !== undefined && (
                        <div
                          className="metric"
                          title={
                            line.endpointEstimatedLocally
                              ? "浏览器本地能量端点到 VAD 端点确认的估算耗时"
                              : "服务端音频终点到 VAD 端点确认的回退估算耗时"
                          }
                        >
                          <span>VAD 端点确认</span>
                          <strong>
                            {formatLatency(line.vadConfirmationLatencyMs)}
                          </strong>
                        </div>
                      )}
                      {line.asrFirstCharacterLatencyMs !== undefined && (
                        <div
                          className="metric"
                          title="VAD 端点确认到首个 ASR 字符的耗时"
                        >
                          <span>ASR 首字</span>
                          <strong>
                            {formatLatency(line.asrFirstCharacterLatencyMs)}
                          </strong>
                        </div>
                      )}
                      {line.asrCompletionLatencyMs !== undefined && (
                        <div
                          className="metric"
                          title="VAD 端点确认到最终转写完成的耗时"
                        >
                          <span>ASR 完成</span>
                          <strong>
                            {formatLatency(line.asrCompletionLatencyMs)}
                          </strong>
                        </div>
                      )}
                      {line.charactersPerSecond !== undefined && (
                        <div
                          className="metric"
                          title="字符数 ÷ VAD 确认到整句完成的 ASR 阶段耗时"
                        >
                          <span>TPS</span>
                          <strong>
                            {line.charactersPerSecond.toFixed(1)} 字/s
                          </strong>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        </article>

        <aside className="event-pane" aria-label="协议事件">
          <button
            className="event-toggle"
            type="button"
            aria-expanded={eventsExpanded}
            onClick={() => setEventsExpanded((expanded) => !expanded)}
          >
            <div>
              <span className="eyebrow">EVENT TRACE</span>
              <h2>协议事件</h2>
            </div>
            <span className="event-toggle-meta">
              <span className="event-count">{events.length}</span>
              <span aria-hidden="true">
                {eventsExpanded ? "收起" : "展开"}
              </span>
            </span>
          </button>
          {eventsExpanded && (
            <div className="event-list">
              {events.length === 0 ? (
                <p className="event-placeholder">
                  事件将在这里按时间倒序出现。
                </p>
              ) : (
                events.map((event) => (
                  <div className="event-row" key={event.id}>
                    <time>{event.time}</time>
                    <div>
                      <strong>{event.type}</strong>
                      {event.detail && <span>{event.detail}</span>}
                    </div>
                  </div>
                ))
              )}
            </div>
          )}
        </aside>
      </section>

      <footer className="controlbar">
        <div className="status-copy">
          <span className={busy ? "pulse" : ""} />
          <strong>{notice}</strong>
        </div>
        <div className="controls">
          {busy ? (
            <button className="primary danger" onClick={stopCurrentStream}>
              停止输入
            </button>
          ) : (
            <button
              className="primary"
              disabled={config === null || microphoneStarting}
              onClick={startMic}
            >
              {microphoneStarting ? "正在启动" : "打开麦克风"}
            </button>
          )}
          <button
            className="secondary"
            disabled={busy || config === null}
            onClick={useSample}
          >
            发送前 30 秒
          </button>
          <button
            className="secondary"
            disabled={busy || config === null}
            onClick={() => fileInputRef.current?.click()}
          >
            选择 WAV
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept="audio/wav,.wav"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) {
                void streamFile(file);
              }
              event.target.value = "";
            }}
          />
          <button
            className="text-button disconnect"
            disabled={connection === "disconnected"}
            onClick={disconnect}
          >
            断开
          </button>
        </div>
      </footer>
    </main>
  );
}

export default App;
