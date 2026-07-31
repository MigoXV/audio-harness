import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

import { config as loadDotenv } from "dotenv";

export type ConnectionMode = "intent" | "model";

export interface DemoConfig {
  apiKey: string;
  baseURL: string;
  model: string;
  connectionMode: ConnectionMode;
  caCertFile?: string;
  tlsCertFile: string;
  tlsKeyFile: string;
  host: string;
  port: number;
  sampleFile: string;
  webDist: string;
}

export interface PublicConfig {
  base_url: string;
  model: string;
  connection_mode: ConnectionMode;
}

export const demoRoot = fileURLToPath(new URL("../../", import.meta.url));
export const projectRoot = resolve(demoRoot, "../..");

function resolveProjectPath(value: string): string {
  return resolve(projectRoot, value);
}

function requireValue(
  env: NodeJS.ProcessEnv,
  name: string,
): string {
  const value = env[name]?.trim();
  if (!value) {
    throw new Error(`必须在 .env 中配置 ${name}`);
  }
  return value;
}

function parsePort(value: string): number {
  const port = Number.parseInt(value, 10);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error("REALTIME_DEMO_PORT 必须是 1 到 65535 之间的整数");
  }
  return port;
}

function parseConnectionMode(value: string): ConnectionMode {
  if (value !== "intent" && value !== "model") {
    throw new Error(
      "OPENAI_REALTIME_CONNECTION_MODE 只能是 intent 或 model",
    );
  }
  return value;
}

export function loadDemoEnv(envFile = resolve(demoRoot, ".env")): void {
  if (existsSync(envFile)) {
    loadDotenv({ path: envFile, override: false, quiet: true });
  }
}

export function configFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): DemoConfig {
  const apiKey = requireValue(env, "OPENAI_API_KEY");
  const rawBaseURL =
    env.OPENAI_BASE_URL?.trim() || "https://api.openai.com/v1";
  const parsedBaseURL = new URL(rawBaseURL);
  if (!["http:", "https:"].includes(parsedBaseURL.protocol)) {
    throw new Error("OPENAI_BASE_URL 必须使用 http 或 https");
  }
  const connectionMode = parseConnectionMode(
    env.OPENAI_REALTIME_CONNECTION_MODE?.trim() || "intent",
  );
  const caCertFile = env.OPENAI_CA_CERT_FILE?.trim();

  return {
    apiKey,
    baseURL: rawBaseURL.replace(/\/+$/, ""),
    model: env.OPENAI_MODEL?.trim() || "gpt-4o-transcribe",
    connectionMode,
    ...(caCertFile
      ? { caCertFile: resolveProjectPath(caCertFile) }
      : {}),
    tlsCertFile: resolveProjectPath(
      env.REALTIME_DEMO_TLS_CERT_FILE?.trim() ||
        "model-bin/realtime-tls/cert.pem",
    ),
    tlsKeyFile: resolveProjectPath(
      env.REALTIME_DEMO_TLS_KEY_FILE?.trim() ||
        "model-bin/realtime-tls/key.pem",
    ),
    host: env.REALTIME_DEMO_HOST?.trim() || "0.0.0.0",
    port: parsePort(env.REALTIME_DEMO_PORT?.trim() || "8090"),
    sampleFile: resolveProjectPath(
      env.REALTIME_DEMO_SAMPLE_FILE?.trim() ||
        "data-bin/audio.wav",
    ),
    webDist: resolveProjectPath(
      env.REALTIME_DEMO_WEB_DIST?.trim() ||
        "examples/openai_realtime_demo/web/dist",
    ),
  };
}

export function publicConfig(config: DemoConfig): PublicConfig {
  return {
    base_url: config.baseURL,
    model: config.model,
    connection_mode: config.connectionMode,
  };
}
