import type { ServerResponse } from "node:http";
import { createServer, type Server } from "node:https";
import { readFileSync } from "node:fs";
import { readFile as readFileAsync } from "node:fs/promises";
import { extname, relative, resolve } from "node:path";

import { WebSocketServer } from "ws";

import {
  type DemoConfig,
  publicConfig,
} from "./config.js";
import { bridgeRealtime } from "./realtime.js";
import { wavExcerpt } from "./wav.js";

const MIME_TYPES: Record<string, string> = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
};

function send(
  response: ServerResponse,
  status: number,
  body: string | Buffer,
  contentType: string,
  headers: Record<string, string> = {},
): void {
  response.writeHead(status, {
    "Content-Type": contentType,
    "Content-Length": Buffer.byteLength(body),
    ...headers,
  });
  response.end(body);
}

function sendJSON(
  response: ServerResponse,
  status: number,
  value: unknown,
): void {
  send(
    response,
    status,
    JSON.stringify(value),
    "application/json; charset=utf-8",
  );
}

async function serveFile(
  response: ServerResponse,
  path: string,
): Promise<void> {
  try {
    const data = await readFileAsync(path);
    send(
      response,
      200,
      data,
      MIME_TYPES[extname(path)] || "application/octet-stream",
    );
  } catch (error) {
    const code =
      typeof error === "object" &&
      error !== null &&
      "code" in error
        ? error.code
        : undefined;
    if (code === "ENOENT") {
      send(response, 404, "Not Found", "text/plain; charset=utf-8");
      return;
    }
    throw error;
  }
}

export function createDemoServer(config: DemoConfig): Server {
  const server = createServer(
    {
      cert: readFileSync(config.tlsCertFile),
      key: readFileSync(config.tlsKeyFile),
    },
    async (request, response) => {
    try {
      const url = new URL(
        request.url || "/",
        `http://${request.headers.host || "localhost"}`,
      );
      if (request.method !== "GET") {
        send(
          response,
          405,
          "Method Not Allowed",
          "text/plain; charset=utf-8",
          { Allow: "GET" },
        );
        return;
      }
      if (url.pathname === "/api/config") {
        sendJSON(response, 200, publicConfig(config));
        return;
      }
      if (url.pathname === "/api/sample") {
        try {
          const excerpt = wavExcerpt(
            await readFileAsync(config.sampleFile),
          );
          send(
            response,
            200,
            excerpt,
            "audio/wav",
            {
              "Content-Disposition":
                'attachment; filename="audio-30s.wav"',
            },
          );
        } catch (error) {
          const code =
            typeof error === "object" &&
            error !== null &&
            "code" in error
              ? error.code
              : undefined;
          send(
            response,
            code === "ENOENT" ? 404 : 422,
            code === "ENOENT"
              ? "测试音频不存在"
              : error instanceof Error
                ? error.message
                : "无法读取测试音频",
            "text/plain; charset=utf-8",
          );
        }
        return;
      }
      if (url.pathname === "/") {
        await serveFile(response, resolve(config.webDist, "index.html"));
        return;
      }
      if (url.pathname.startsWith("/assets/")) {
        const requested = resolve(
          config.webDist,
          `.${decodeURIComponent(url.pathname)}`,
        );
        const withinDist = relative(config.webDist, requested);
        if (
          withinDist.startsWith("..") ||
          withinDist.includes("\0")
        ) {
          send(response, 404, "Not Found", "text/plain; charset=utf-8");
          return;
        }
        await serveFile(response, requested);
        return;
      }
      send(response, 404, "Not Found", "text/plain; charset=utf-8");
    } catch {
      send(
        response,
        500,
        "Internal Server Error",
        "text/plain; charset=utf-8",
      );
    }
    },
  );

  const sockets = new WebSocketServer({
    noServer: true,
    maxPayload: 16 * 1024 * 1024,
  });
  server.on("upgrade", (request, socket, head) => {
    const url = new URL(
      request.url || "/",
      `http://${request.headers.host || "localhost"}`,
    );
    if (url.pathname !== "/ws") {
      socket.destroy();
      return;
    }
    sockets.handleUpgrade(request, socket, head, (browser) => {
      bridgeRealtime(browser, config);
    });
  });
  server.on("close", () => {
    sockets.close();
  });
  return server;
}
