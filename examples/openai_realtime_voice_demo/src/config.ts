import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { config as loadDotenv } from "dotenv";

export interface VoiceDemoConfig {
  apiKey: string;
  baseURL: string;
  model: string;
  caCertFile?: string;
  voice: string;
  instructions: string;
  tlsCertFile: string;
  tlsKeyFile: string;
  host: string;
  port: number;
  webRoot: string;
}

export interface PublicVoiceDemoConfig {
  base_url: string;
  model: string;
  voice: string;
}

export const demoRoot = fileURLToPath(new URL("../", import.meta.url));
export const projectRoot = resolve(demoRoot, "../..");

function projectPath(value: string): string {
  return resolve(projectRoot, value);
}

function required(env: NodeJS.ProcessEnv, name: string): string {
  const value = env[name]?.trim();
  if (!value) {
    throw new Error(`必须在 .env 中配置 ${name}`);
  }
  return value;
}

function parsePort(value: string): number {
  const port = Number.parseInt(value, 10);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error("VOICE_DEMO_PORT 必须是 1 到 65535 之间的整数");
  }
  return port;
}

export function loadVoiceDemoEnv(
  envFile = resolve(demoRoot, ".env"),
): void {
  if (existsSync(envFile)) {
    loadDotenv({ path: envFile, override: false, quiet: true });
  }
}

export function configFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): VoiceDemoConfig {
  const rawBaseURL =
    env.REALTIME_BASE_URL?.trim() || "https://127.0.0.1:8000/v1";
  const baseURL = new URL(rawBaseURL);
  if (!["http:", "https:"].includes(baseURL.protocol)) {
    throw new Error("REALTIME_BASE_URL 必须使用 HTTP(S)");
  }
  const caCertFile = env.REALTIME_CA_CERT_FILE?.trim();
  return {
    apiKey: required(env, "REALTIME_API_KEY"),
    baseURL: rawBaseURL.replace(/\/+$/, ""),
    model: required(env, "REALTIME_MODEL"),
    ...(caCertFile ? { caCertFile: projectPath(caCertFile) } : {}),
    voice: env.REALTIME_VOICE?.trim() || "wise",
    instructions:
      env.REALTIME_INSTRUCTIONS?.trim() ||
      "你是一个自然、简洁、有帮助的中文语音助手。",
    tlsCertFile: projectPath(
      env.VOICE_DEMO_TLS_CERT_FILE?.trim() ||
        "model-bin/realtime-tls/cert.pem",
    ),
    tlsKeyFile: projectPath(
      env.VOICE_DEMO_TLS_KEY_FILE?.trim() ||
        "model-bin/realtime-tls/key.pem",
    ),
    host: env.VOICE_DEMO_HOST?.trim() || "0.0.0.0",
    port: parsePort(env.VOICE_DEMO_PORT?.trim() || "8091"),
    webRoot: resolve(demoRoot, "web"),
  };
}

export function publicConfig(
  config: VoiceDemoConfig,
): PublicVoiceDemoConfig {
  return {
    base_url: config.baseURL,
    model: config.model,
    voice: config.voice,
  };
}
