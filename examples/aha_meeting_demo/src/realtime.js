import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

import WebSocket from "ws";

const INPUT_SAMPLE_RATE = 16_000;

export function realtimeURL(config) {
  const url = new URL(config.baseURL);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/realtime`;
  url.search = "intent=transcription&x_aha=v1";
  return url;
}

export function sessionUpdate(config) {
  return {
    type: "session.update",
    event_id: `cfg_${randomUUID()}`,
    session: {
      type: "transcription",
      audio: {
        input: {
          format: { type: "audio/pcm", rate: INPUT_SAMPLE_RATE },
          transcription: { model: config.model, language: "zh-CN" },
          // 协议层由 server_vad 接收自动断句；Workflow 的 semantic_vad 决定正式结束边界。
          turn_detection: { type: "server_vad" },
        },
      },
    },
  };
}

export function createLifecycle(config) {
  let configSent = false;
  let ready = false;
  return {
    handle(event) {
      if (event.type === "session.created" && !configSent) {
        configSent = true;
        return [{ type: "send_config", event: sessionUpdate(config) }];
      }
      if (event.type === "session.updated" && configSent && !ready) {
        ready = true;
        return [{ type: "ready" }];
      }
      return [{ type: "relay", event }];
    },
    get ready() { return ready; },
  };
}

function sendJSON(socket, value) {
  if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(value));
}

function appendAudio(upstream, pcm) {
  if (pcm.length === 0 || pcm.length % 2 !== 0) return;
  sendJSON(upstream, {
    type: "input_audio_buffer.append",
    event_id: `audio_${randomUUID()}`,
    audio: Buffer.from(pcm).toString("base64"),
  });
}

function appendTrailingSilence(upstream) {
  const frame = Buffer.alloc(INPUT_SAMPLE_RATE * 2 / 10); // 100 ms, 16 kHz mono PCM S16LE
  for (let index = 0; index < 20; index += 1) appendAudio(upstream, frame);
}

export function finishAudioInput(upstream) {
  appendTrailingSilence(upstream);
  sendJSON(upstream, {
    type: "input_audio_buffer.commit",
    event_id: `commit_${randomUUID()}`,
  });
}

export function connectRealtime(browser, config) {
  let ca;
  try {
    ca = config.caCertFile ? readFileSync(config.caCertFile) : undefined;
  } catch {
    sendJSON(browser, { type: "error", error: { message: "无法读取 Realtime CA 证书" } });
    browser.close(1011, "CA 证书不可用");
    return;
  }
  const upstream = new WebSocket(realtimeURL(config), {
    headers: { Authorization: `Bearer ${config.apiKey}` },
    rejectUnauthorized: true,
    ...(ca ? { ca } : {}),
  });
  const lifecycle = createLifecycle(config);
  let closing = false;
  let inputFinished = false;
  const closeBoth = (code, reason) => {
    if (closing) return;
    closing = true;
    if (browser.readyState <= WebSocket.OPEN) browser.close(code, reason);
    if (upstream.readyState <= WebSocket.OPEN) upstream.close(code, reason);
  };

  upstream.on("message", (data) => {
    try {
      const event = JSON.parse(data.toString());
      for (const action of lifecycle.handle(event)) {
        if (action.type === "send_config") sendJSON(upstream, action.event);
        if (action.type === "ready") sendJSON(browser, { type: "demo.ready" });
        if (action.type === "relay") sendJSON(browser, action.event);
      }
    } catch {
      sendJSON(browser, { type: "error", error: { message: "上游事件不是合法 JSON" } });
    }
  });
  browser.on("message", (data, isBinary) => {
    if (isBinary) {
      if (!lifecycle.ready) {
        sendJSON(browser, { type: "error", error: { message: "Realtime 会话尚未配置完成" } });
      } else {
        appendAudio(upstream, data);
      }
      return;
    }
    try {
      const control = JSON.parse(data.toString());
      if (control.type === "stop" && !inputFinished) {
        inputFinished = true;
        finishAudioInput(upstream);
      }
      if (control.type === "disconnect") closeBoth(1000, "浏览器主动断开");
    } catch {
      sendJSON(browser, { type: "error", error: { message: "浏览器控制消息不合法" } });
    }
  });
  upstream.on("error", (error) => {
    const detail = error instanceof Error && error.message ? `：${error.message}` : "";
    sendJSON(browser, { type: "error", error: { message: `Realtime 上游连接失败${detail}` } });
    closeBoth(1011, "上游连接失败");
  });
  upstream.on("close", () => closeBoth(1000, "上游连接关闭"));
  browser.on("close", () => closeBoth(1000, "浏览器连接关闭"));
}
