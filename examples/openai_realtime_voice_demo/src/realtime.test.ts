import { describe, expect, test } from "vitest";

import { configFromEnv, type VoiceDemoConfig } from "./config.js";
import {
  parseBrowserControl,
  realtimeURL,
  sessionUpdate,
} from "./realtime.js";

const config: VoiceDemoConfig = {
  apiKey: "secret",
  baseURL: "https://127.0.0.1:8000/v1",
  model: "voice-agent",
  voice: "wise",
  instructions: "测试助手",
  tlsCertFile: "cert.pem",
  tlsKeyFile: "key.pem",
  host: "127.0.0.1",
  port: 8091,
  webRoot: "web",
};

describe("server-managed Realtime protocol", () => {
  test("defaults to the local Audio Harness data plane", () => {
    const local = configFromEnv({
      REALTIME_API_KEY: "local-demo-key",
      REALTIME_MODEL: "voice-agent",
    });
    expect(local.baseURL).toBe("https://127.0.0.1:8000/v1");
  });

  test("builds a model-routed Realtime URL", () => {
    expect(realtimeURL(config).toString()).toBe(
      "wss://127.0.0.1:8000/v1/realtime?model=voice-agent",
    );
  });

  test("creates the audio conversation session on the server", () => {
    const event = sessionUpdate(config);
    expect(event.type).toBe("session.update");
    expect(event.session).toMatchObject({
      type: "realtime",
      output_modalities: ["audio"],
      audio: { output: { voice: "wise" } },
    });
    expect(JSON.stringify(event)).not.toMatch(/audio_harness\.|x_aha\./);
  });

  test("does not accept raw Realtime events from the browser", () => {
    expect(parseBrowserControl(Buffer.from('{"type":"stop"}'))).toEqual({
      type: "stop",
    });
    expect(() => parseBrowserControl(
      Buffer.from('{"type":"session.update"}'),
    )).toThrow(/只允许/);
  });
});
