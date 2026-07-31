import { describe, expect, it } from "vitest";

import {
  configFromEnv,
  publicConfig,
} from "./config.js";
import { testing } from "./realtime.js";

describe("Demo config", () => {
  it("keeps the key out of public config", () => {
    const config = configFromEnv({
      OPENAI_API_KEY: "test-secret",
      OPENAI_BASE_URL: "https://api.openai.com/v1",
    });

    expect(publicConfig(config)).toEqual({
      base_url: "https://api.openai.com/v1",
      model: "gpt-4o-transcribe",
      connection_mode: "intent",
    });
    expect(JSON.stringify(publicConfig(config))).not.toContain(
      "test-secret",
    );
    expect(config.host).toBe("0.0.0.0");
  });

  it("builds official intent and local model URLs separately", () => {
    const official = configFromEnv({
      OPENAI_API_KEY: "test-secret",
      OPENAI_BASE_URL: "https://api.openai.com/v1",
    });
    const local = configFromEnv({
      OPENAI_API_KEY: "test-secret",
      OPENAI_BASE_URL: "https://127.0.0.1:8000/v1",
      OPENAI_REALTIME_CONNECTION_MODE: "model",
    });

    expect(testing.realtimeURL(official).toString()).toBe(
      "wss://api.openai.com/v1/realtime?intent=transcription",
    );
    expect(testing.realtimeURL(local).toString()).toBe(
      "wss://127.0.0.1:8000/v1/realtime?model=gpt-4o-transcribe",
    );
  });
});
