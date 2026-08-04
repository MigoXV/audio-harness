import { readFileSync } from "node:fs";
import { Agent as HttpsAgent } from "node:https";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

const demoRoot = fileURLToPath(new URL("../", import.meta.url));
const projectRoot = resolve(demoRoot, "../..");

export default defineConfig(({ command, mode }) => {
  const common = {
    plugins: [react()],
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
      css: true,
    },
  };
  if (command !== "serve" || mode === "test") {
    return common;
  }

  const env = {
    ...loadEnv(mode, projectRoot, ""),
    ...loadEnv(mode, demoRoot, ""),
  };
  const certPath = resolve(
    projectRoot,
    env.REALTIME_DEMO_TLS_CERT_FILE ||
      env.HTTP_TLS_CERT_FILE ||
      "model-bin/realtime-tls/cert.pem",
  );
  const keyPath = resolve(
    projectRoot,
    env.REALTIME_DEMO_TLS_KEY_FILE ||
      env.HTTP_TLS_KEY_FILE ||
      "model-bin/realtime-tls/key.pem",
  );
  const cert = readFileSync(certPath);
  const key = readFileSync(keyPath);
  const backendTarget = `https://127.0.0.1:${
    env.REALTIME_DEMO_PORT || "8090"
  }`;
  const backendAgent = new HttpsAgent({ ca: cert });

  return {
    ...common,
    server: {
      host: env.REALTIME_DEMO_HOST || "0.0.0.0",
      port: 5174,
      https: { cert, key },
      proxy: {
        "/api": {
          target: backendTarget,
          secure: true,
          agent: backendAgent,
        },
        "/ws": {
          target: backendTarget,
          secure: true,
          agent: backendAgent,
          ws: true,
        },
      },
    },
  };
});
