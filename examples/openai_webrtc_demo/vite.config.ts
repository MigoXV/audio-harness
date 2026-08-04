import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { Agent, fetch, FormData } from "undici";
import { defineConfig, loadEnv, type Plugin } from "vite";

const MAX_BODY_BYTES = 2 * 1024 * 1024;

async function readJson(request: NodeJS.ReadableStream): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.length;
    if (size > MAX_BODY_BYTES) {
      throw new Error("请求体过大");
    }
    chunks.push(bytes);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function json(response: import("node:http").ServerResponse, status: number, body: unknown) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(body));
}

function apiPlugin(
  env: Record<string, string>,
  projectRoot: string,
): Plugin {
  const apiKey = (
    env.OPENAI_API_KEY ||
    env.REALTIME_API_KEY
  )?.trim();
  const baseURL = (
    env.OPENAI_BASE_URL ||
    "https://127.0.0.1:8000/v1"
  )
    .replace(/\/+$/, "");
  const model = env.OPENAI_MODEL?.trim() || "gpt-4o-transcribe";
  const caPath = env.OPENAI_CA_CERT_FILE?.trim();
  const rootCertPath =
    env.HTTP_TLS_CERT_FILE?.trim() ||
    "model-bin/realtime-tls/cert.pem";
  const iceServers = JSON.parse(
    env.WEBRTC_DEMO_ICE_SERVERS_JSON ||
      env.WEBRTC_ICE_SERVERS_JSON ||
      "[]",
  ) as unknown;
  if (!Array.isArray(iceServers)) {
    throw new Error("WEBRTC_DEMO_ICE_SERVERS_JSON 必须是 JSON 数组");
  }
  const dispatcher = new Agent({
    connect: {
      ca: readFileSync(
        caPath
          ? resolve(process.cwd(), caPath)
          : resolve(projectRoot, rootCertPath),
        "utf8",
      ),
    },
  });

  return {
    name: "audio-harness-webrtc-api",
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        if (
          request.method === "GET" &&
          request.url === "/api/config"
        ) {
          json(response, 200, { ice_servers: iceServers });
          return;
        }
        if (request.method !== "POST") {
          next();
          return;
        }
        try {
          if (!apiKey) {
            json(response, 500, {
              error:
                "请在当前 Demo 配置 OPENAI_API_KEY，或在项目根目录配置 " +
                "REALTIME_API_KEY",
            });
            return;
          }
          if (request.url === "/api/realtime/calls") {
            const payload = (await readJson(request)) as { sdp?: unknown };
            if (typeof payload.sdp !== "string" || !payload.sdp.trim()) {
              json(response, 400, { error: "sdp 不能为空" });
              return;
            }
            const form = new FormData();
            form.set("sdp", payload.sdp);
            form.set(
              "session",
              JSON.stringify({
                type: "transcription",
                audio: {
                  input: {
                    format: { type: "audio/pcm", rate: 24000 },
                    transcription: { model, language: "zh-CN" },
                    turn_detection: { type: "server_vad" },
                  },
                },
              }),
            );
            const upstream = await fetch(`${baseURL}/realtime/calls`, {
              method: "POST",
              headers: { Authorization: `Bearer ${apiKey}` },
              body: form,
              dispatcher,
            });
            const answer = await upstream.text();
            if (!upstream.ok) {
              json(response, upstream.status, { error: answer });
              return;
            }
            json(response, 201, {
              sdp: answer,
              location: upstream.headers.get("location"),
            });
            return;
          }
          if (request.url === "/api/realtime/hangup") {
            const payload = (await readJson(request)) as {
              location?: unknown;
            };
            const callId =
              typeof payload.location === "string"
                ? payload.location.match(/\/calls\/([^/]+)$/)?.[1]
                : undefined;
            if (!callId) {
              json(response, 400, { error: "无效的 call location" });
              return;
            }
            const upstream = await fetch(
              `${baseURL}/realtime/calls/${encodeURIComponent(callId)}/hangup`,
              {
                method: "POST",
                headers: { Authorization: `Bearer ${apiKey}` },
                dispatcher,
              },
            );
            response.writeHead(upstream.status);
            response.end();
            return;
          }
          next();
        } catch (error) {
          json(response, 502, {
            error: error instanceof Error ? error.message : "上游请求失败",
          });
        }
      });
    },
  };
}

export default defineConfig(({ command, mode }) => {
  const projectRoot = resolve(process.cwd(), "../..");
  const rootEnv = loadEnv(mode, projectRoot, "");
  const env = {
    ...rootEnv,
    ...loadEnv(mode, process.cwd(), ""),
  };
  if (command !== "serve") {
    return {};
  }
  const cert = readFileSync(
    resolve(
      projectRoot,
      env.WEBRTC_DEMO_TLS_CERT_FILE ||
        env.HTTP_TLS_CERT_FILE ||
        "model-bin/realtime-tls/cert.pem",
    ),
  );
  const key = readFileSync(
    resolve(
      projectRoot,
      env.WEBRTC_DEMO_TLS_KEY_FILE ||
        env.HTTP_TLS_KEY_FILE ||
        "model-bin/realtime-tls/key.pem",
    ),
  );
  return {
    plugins: [apiPlugin(env, projectRoot)],
    server: {
      host: env.WEBRTC_DEMO_HOST || "0.0.0.0",
      port: Number(env.WEBRTC_DEMO_PORT || 5175),
      https: { cert, key },
    },
  };
});
