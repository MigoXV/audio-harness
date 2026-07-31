import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

import { HttpsProxyAgent } from "https-proxy-agent";
import type {
  RealtimeClientEvent,
  RealtimeErrorEvent,
} from "openai/resources/realtime/realtime";
import { getProxyForUrl } from "proxy-from-env";
import WebSocket, {
  type Data,
  type RawData,
  type WebSocket as WebSocketType,
} from "ws";

import type { DemoConfig } from "./config.js";

function realtimeURL(config: DemoConfig): URL {
  const url = new URL(config.baseURL);
  url.protocol = url.protocol === "http:" ? "ws:" : "wss:";
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/realtime`;
  url.search = "";
  url.hash = "";
  if (config.connectionMode === "intent") {
    url.searchParams.set("intent", "transcription");
  } else {
    url.searchParams.set("model", config.model);
  }
  return url;
}

function errorEvent(message: string, code: string): RealtimeErrorEvent {
  return {
    type: "error",
    event_id: `event_${randomUUID().replaceAll("-", "")}`,
    error: {
      type: "server_error",
      code,
      message,
    },
  };
}

function parseClientEvent(data: RawData): RealtimeClientEvent {
  const parsed: unknown = JSON.parse(data.toString());
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    !("type" in parsed) ||
    typeof parsed.type !== "string"
  ) {
    throw new Error("Realtime 客户端事件缺少字符串 type");
  }
  return parsed as RealtimeClientEvent;
}

function sendJSON(socket: WebSocketType, value: unknown): void {
  if (socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(value));
  }
}

export function bridgeRealtime(
  browser: WebSocketType,
  config: DemoConfig,
): void {
  const url = realtimeURL(config);
  const proxyLookupURL = new URL(url);
  proxyLookupURL.protocol = url.protocol === "wss:" ? "https:" : "http:";
  const proxyURL = getProxyForUrl(proxyLookupURL.toString());
  let ca: Buffer | undefined;
  try {
    ca = config.caCertFile
      ? readFileSync(config.caCertFile)
      : undefined;
  } catch {
    sendJSON(
      browser,
      errorEvent(
        "无法读取 Realtime CA 证书",
        "ca_cert_read_failed",
      ),
    );
    browser.close(1011, "CA 证书不可用");
    return;
  }
  const upstream = new WebSocket(url, {
    headers: {
      Authorization: `Bearer ${config.apiKey}`,
    },
    rejectUnauthorized: true,
    ...(proxyURL
      ? { agent: new HttpsProxyAgent(proxyURL) }
      : {}),
    ...(ca ? { ca } : {}),
  });
  let closing = false;

  const closeBoth = (code = 1000, reason = "连接关闭") => {
    if (closing) {
      return;
    }
    closing = true;
    if (
      browser.readyState === WebSocket.OPEN ||
      browser.readyState === WebSocket.CONNECTING
    ) {
      browser.close(code, reason);
    }
    if (
      upstream.readyState === WebSocket.OPEN ||
      upstream.readyState === WebSocket.CONNECTING
    ) {
      upstream.close(code, reason);
    }
  };

  upstream.on("message", (data: Data, isBinary: boolean) => {
    if (browser.readyState === WebSocket.OPEN) {
      browser.send(data, { binary: isBinary });
    }
  });

  browser.on("message", (data: RawData) => {
    if (upstream.readyState !== WebSocket.OPEN) {
      sendJSON(
        browser,
        errorEvent(
          "Realtime 上游尚未就绪",
          "upstream_not_ready",
        ),
      );
      return;
    }
    try {
      const event = parseClientEvent(data);
      upstream.send(JSON.stringify(event));
    } catch (error) {
      sendJSON(
        browser,
        errorEvent(
          error instanceof Error
            ? error.message
            : "Realtime 客户端事件不合法",
          "invalid_client_event",
        ),
      );
    }
  });

  upstream.on("error", (error) => {
    const detail =
      error instanceof Error
        ? `${error.name}: ${error.message}`
        : String(error);
    console.error(`Realtime 上游连接错误：${detail}`);
    sendJSON(
      browser,
      errorEvent(
        "Realtime 上游连接失败，请检查服务配置与证书",
        "upstream_connection_failed",
      ),
    );
    closeBoth(1011, "上游连接失败");
  });
  upstream.on("unexpected-response", (_request, response) => {
    sendJSON(
      browser,
      errorEvent(
        `Realtime 上游拒绝连接（HTTP ${response.statusCode}）`,
        "upstream_rejected",
      ),
    );
    closeBoth(1011, "上游拒绝连接");
  });
  browser.on("error", () => closeBoth(1011, "浏览器连接失败"));
  upstream.on("close", (code, reason) => {
    closeBoth(
      code === 1000 ? 1000 : 1011,
      reason.toString() || "上游连接关闭",
    );
  });
  browser.on("close", () => closeBoth(1000, "浏览器连接关闭"));
}

export const testing = {
  realtimeURL,
  errorEvent,
  parseClientEvent,
};
