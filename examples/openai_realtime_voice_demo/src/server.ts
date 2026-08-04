import { readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import type { ServerResponse } from "node:http";
import { createServer, type Server } from "node:https";
import { extname, relative, resolve } from "node:path";

import { WebSocketServer } from "ws";

import {
  type VoiceDemoConfig,
  publicConfig,
} from "./config.js";
import { serveVoiceConversation } from "./realtime.js";

const CONTENT_TYPES: Record<string, string> = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
};

function send(
  response: ServerResponse,
  status: number,
  body: string | Buffer,
  contentType: string,
): void {
  response.writeHead(status, {
    "Content-Type": contentType,
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  response.end(body);
}

async function serveFile(
  response: ServerResponse,
  path: string,
): Promise<void> {
  try {
    const body = await readFile(path);
    send(
      response,
      200,
      body,
      CONTENT_TYPES[extname(path)] || "application/octet-stream",
    );
  } catch {
    send(response, 404, "Not Found", "text/plain; charset=utf-8");
  }
}

export function createVoiceDemoServer(config: VoiceDemoConfig): Server {
  const server = createServer(
    {
      cert: readFileSync(config.tlsCertFile),
      key: readFileSync(config.tlsKeyFile),
    },
    async (request, response) => {
      const url = new URL(
        request.url || "/",
        `https://${request.headers.host || "localhost"}`,
      );
      if (request.method !== "GET") {
        send(response, 405, "Method Not Allowed", "text/plain; charset=utf-8");
        return;
      }
      if (url.pathname === "/api/config") {
        send(
          response,
          200,
          JSON.stringify(publicConfig(config)),
          "application/json; charset=utf-8",
        );
        return;
      }
      const relativePath = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
      const path = resolve(config.webRoot, decodeURIComponent(relativePath));
      const withinRoot = relative(config.webRoot, path);
      if (withinRoot.startsWith("..") || withinRoot.includes("\0")) {
        send(response, 404, "Not Found", "text/plain; charset=utf-8");
        return;
      }
      await serveFile(response, path);
    },
  );
  const sockets = new WebSocketServer({
    noServer: true,
    maxPayload: 4 * 1024 * 1024,
  });
  server.on("upgrade", (request, socket, head) => {
    const url = new URL(
      request.url || "/",
      `https://${request.headers.host || "localhost"}`,
    );
    if (url.pathname !== "/voice") {
      socket.destroy();
      return;
    }
    sockets.handleUpgrade(request, socket, head, (browser) => {
      serveVoiceConversation(browser, config);
    });
  });
  server.on("close", () => sockets.close());
  return server;
}
