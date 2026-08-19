import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { config as loadDotenv } from "dotenv";

export const demoRoot = fileURLToPath(new URL("../", import.meta.url));
export const projectRoot = resolve(demoRoot, "../..");

function required(env, name) {
  const value = env[name]?.trim();
  if (!value) throw new Error(`必须在 .env 中配置 ${name}`);
  return value;
}

function port(value) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65_535) {
    throw new Error("MEETING_DEMO_PORT 必须是 1 到 65535 之间的整数");
  }
  return parsed;
}

export function loadDemoEnv() {
  const envFile = resolve(demoRoot, ".env");
  if (existsSync(envFile)) loadDotenv({ path: envFile, override: false, quiet: true });
}

export function configFromEnv(env = process.env) {
  const rawBaseURL = (env.REALTIME_BASE_URL || "https://127.0.0.1:8000/v1").replace(/\/+$/, "");
  const url = new URL(rawBaseURL);
  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error("REALTIME_BASE_URL 必须使用 HTTP(S)");
  }
  const projectPath = (value) => resolve(projectRoot, value);
  const caCertFile = env.REALTIME_CA_CERT_FILE?.trim();
  return {
    apiKey: required(env, "REALTIME_API_KEY"),
    baseURL: rawBaseURL,
    model: required(env, "REALTIME_MODEL"),
    caCertFile: caCertFile ? projectPath(caCertFile) : undefined,
    tlsCertFile: projectPath(env.MEETING_DEMO_TLS_CERT_FILE?.trim() || "model-bin/realtime-tls/cert.pem"),
    tlsKeyFile: projectPath(env.MEETING_DEMO_TLS_KEY_FILE?.trim() || "model-bin/realtime-tls/key.pem"),
    host: env.MEETING_DEMO_HOST?.trim() || "0.0.0.0",
    port: port(env.MEETING_DEMO_PORT?.trim() || "8092"),
    webRoot: resolve(demoRoot, "web"),
  };
}

export function publicConfig(config) {
  return { base_url: config.baseURL, model: config.model };
}
