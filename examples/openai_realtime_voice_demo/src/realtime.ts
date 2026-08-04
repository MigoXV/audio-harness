import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

import { HttpsProxyAgent } from "https-proxy-agent";
import { getProxyForUrl } from "proxy-from-env";
import WebSocket, {
  type Data,
  type RawData,
  type WebSocket as WebSocketType,
} from "ws";

import type { VoiceDemoConfig } from "./config.js";

type BrowserControl =
  | { type: "stop" }
  | { type: "clear" }
  | { type: "disconnect" };

type RealtimeEvent = Record<string, unknown> & { type: string };

function eventID(): string {
  return `event_${randomUUID().replaceAll("-", "")}`;
}

export function realtimeURL(config: VoiceDemoConfig): URL {
  const url = new URL(config.baseURL);
  url.protocol = url.protocol === "http:" ? "ws:" : "wss:";
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/realtime`;
  url.search = "";
  url.hash = "";
  url.searchParams.set("model", config.model);
  return url;
}

export function sessionUpdate(config: VoiceDemoConfig): RealtimeEvent {
  return {
    type: "session.update",
    event_id: eventID(),
    session: {
      type: "realtime",
      instructions: config.instructions,
      output_modalities: ["audio"],
      audio: {
        input: {
          format: { type: "audio/pcm", rate: 24_000 },
          turn_detection: {
            type: "server_vad",
            create_response: true,
            interrupt_response: true,
          },
        },
        output: {
          format: { type: "audio/pcm", rate: 24_000 },
          voice: config.voice,
        },
      },
    },
  };
}

function rawBuffer(data: RawData): Buffer {
  if (Array.isArray(data)) {
    return Buffer.concat(data);
  }
  if (data instanceof ArrayBuffer) {
    return Buffer.from(data);
  }
  return Buffer.from(data);
}

export function parseBrowserControl(data: RawData): BrowserControl {
  const value: unknown = JSON.parse(rawBuffer(data).toString());
  if (typeof value !== "object" || value === null || !("type" in value)) {
    throw new Error("控制消息缺少 type");
  }
  const type = value.type;
  if (type !== "stop" && type !== "clear" && type !== "disconnect") {
    throw new Error("只允许 stop、clear 或 disconnect 控制消息");
  }
  return { type };
}

function sendJSON(socket: WebSocketType, value: unknown): void {
  if (socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(value));
  }
}

function appendAudio(upstream: WebSocketType, pcm: Buffer): void {
  if (pcm.length === 0) {
    return;
  }
  if (pcm.length % 2 !== 0) {
    throw new Error("PCM S16LE 字节数必须为偶数");
  }
  upstream.send(JSON.stringify({
    type: "input_audio_buffer.append",
    event_id: eventID(),
    audio: pcm.toString("base64"),
  }));
}

function appendTrailingSilence(upstream: WebSocketType): void {
  const frame = Buffer.alloc(24_000 * 2 / 10);
  for (let index = 0; index < 20; index += 1) {
    appendAudio(upstream, frame);
  }
}

function stringField(event: RealtimeEvent, name: string): string {
  return typeof event[name] === "string" ? event[name] : "";
}

function relayServerEvent(
  browser: WebSocketType,
  event: RealtimeEvent,
): void {
  switch (event.type) {
    case "session.updated":
      sendJSON(browser, { type: "ready" });
      break;
    case "input_audio_buffer.speech_started":
      sendJSON(browser, { type: "speech_started" });
      break;
    case "input_audio_buffer.speech_stopped":
      sendJSON(browser, { type: "speech_stopped" });
      break;
    case "conversation.item.input_audio_transcription.completed":
      sendJSON(browser, {
        type: "user_transcript",
        text: stringField(event, "transcript"),
      });
      break;
    case "response.output_audio_transcript.delta":
      sendJSON(browser, {
        type: "assistant_delta",
        delta: stringField(event, "delta"),
      });
      break;
    case "response.output_audio_transcript.done":
      sendJSON(browser, {
        type: "assistant_done",
        text: stringField(event, "transcript"),
      });
      break;
    case "response.output_audio.delta": {
      const audio = stringField(event, "delta");
      if (audio && browser.readyState === WebSocket.OPEN) {
        browser.send(Buffer.from(audio, "base64"), { binary: true });
      }
      break;
    }
    case "response.created":
      sendJSON(browser, { type: "response_started" });
      break;
    case "response.done": {
      const response = event.response;
      const status =
        typeof response === "object" && response !== null &&
        "status" in response && typeof response.status === "string"
          ? response.status
          : "unknown";
      sendJSON(browser, { type: "response_done", status });
      break;
    }
    case "error": {
      const error = event.error;
      const message =
        typeof error === "object" && error !== null &&
        "message" in error && typeof error.message === "string"
          ? error.message
          : "Realtime 上游返回错误";
      sendJSON(browser, { type: "error", message });
      break;
    }
  }
}

export function serveVoiceConversation(
  browser: WebSocketType,
  config: VoiceDemoConfig,
): void {
  const url = realtimeURL(config);
  const proxyLookupURL = new URL(url);
  proxyLookupURL.protocol = url.protocol === "wss:" ? "https:" : "http:";
  const proxyURL = getProxyForUrl(proxyLookupURL.toString());
  let ca: Buffer | undefined;
  try {
    ca = config.caCertFile ? readFileSync(config.caCertFile) : undefined;
  } catch {
    sendJSON(browser, { type: "error", message: "无法读取 Realtime CA 证书" });
    browser.close(1011, "CA 证书不可用");
    return;
  }
  const upstream = new WebSocket(url, {
    headers: { Authorization: `Bearer ${config.apiKey}` },
    rejectUnauthorized: true,
    ...(proxyURL ? { agent: new HttpsProxyAgent(proxyURL) } : {}),
    ...(ca ? { ca } : {}),
  });
  let closing = false;

  const closeBoth = (code = 1000, reason = "连接关闭") => {
    if (closing) {
      return;
    }
    closing = true;
    if (browser.readyState <= WebSocket.OPEN) {
      browser.close(code, reason);
    }
    if (upstream.readyState <= WebSocket.OPEN) {
      upstream.close(code, reason);
    }
  };

  upstream.on("open", () => {
    upstream.send(JSON.stringify(sessionUpdate(config)));
  });
  upstream.on("message", (data: Data) => {
    try {
      const event = JSON.parse(data.toString()) as RealtimeEvent;
      relayServerEvent(browser, event);
    } catch {
      sendJSON(browser, { type: "error", message: "上游事件不是合法 JSON" });
    }
  });
  browser.on("message", (data: RawData, isBinary: boolean) => {
    if (upstream.readyState !== WebSocket.OPEN) {
      sendJSON(browser, { type: "error", message: "Realtime 上游尚未就绪" });
      return;
    }
    try {
      if (isBinary) {
        appendAudio(upstream, rawBuffer(data));
        return;
      }
      const control = parseBrowserControl(data);
      if (control.type === "stop") {
        appendTrailingSilence(upstream);
      } else if (control.type === "clear") {
        upstream.send(JSON.stringify({
          type: "input_audio_buffer.clear",
          event_id: eventID(),
        }));
      } else {
        closeBoth(1000, "浏览器主动断开");
      }
    } catch (error) {
      sendJSON(browser, {
        type: "error",
        message: error instanceof Error ? error.message : "客户端消息不合法",
      });
    }
  });
  upstream.on("error", () => {
    sendJSON(browser, { type: "error", message: "Realtime 上游连接失败" });
    closeBoth(1011, "上游连接失败");
  });
  upstream.on("unexpected-response", (_request, response) => {
    sendJSON(browser, {
      type: "error",
      message: `Realtime 上游拒绝连接（HTTP ${response.statusCode}）`,
    });
    closeBoth(1011, "上游拒绝连接");
  });
  upstream.on("close", () => closeBoth(1000, "上游连接关闭"));
  browser.on("close", () => closeBoth(1000, "浏览器连接关闭"));
  browser.on("error", () => closeBoth(1011, "浏览器连接失败"));
}
