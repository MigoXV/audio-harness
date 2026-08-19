import { readFileSync } from "node:fs";
import { createServer as createHttpsServer } from "node:https";
import { extname, resolve } from "node:path";

import { WebSocketServer } from "ws";

import { publicConfig } from "./config.js";
import { connectRealtime, realtimeURL } from "./realtime.js";

const MIME_TYPES = { ".css": "text/css", ".html": "text/html", ".js": "text/javascript" };

export function createMeetingDemoServer(config) {
  const server = createHttpsServer({ key: readFileSync(config.tlsKeyFile), cert: readFileSync(config.tlsCertFile) }, (request, response) => {
    const url = new URL(request.url, "https://demo.local");
    if (url.pathname === "/api/config") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        ...publicConfig(config),
        upstream_url: realtimeURL(config).toString(),
      }));
      return;
    }
    const requested = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
    const filename = resolve(config.webRoot, requested);
    if (!filename.startsWith(`${config.webRoot}/`)) {
      response.writeHead(403).end();
      return;
    }
    try {
      const content = readFileSync(filename);
      response.writeHead(200, { "content-type": MIME_TYPES[extname(filename)] || "application/octet-stream" });
      response.end(content);
    } catch {
      response.writeHead(404);
      response.end("Not found");
    }
  });
  const websocket = new WebSocketServer({ server, path: "/ws" });
  websocket.on("connection", (browser) => connectRealtime(browser, config));
  return server;
}
