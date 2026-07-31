import { describe, expect, it } from "vitest";

import {
  floatToPcm16,
  lastVoicedOffsetMilliseconds,
  pcm16ToBase64,
  silenceBase64,
  StreamingResampler,
} from "./audio";

describe("audio helpers", () => {
  it("converts normalized floats to PCM16", () => {
    expect(Array.from(floatToPcm16(Float32Array.from([-1, 0, 1])))).toEqual([
      -32768, 0, 32767,
    ]);
  });

  it("encodes the underlying PCM bytes", () => {
    expect(pcm16ToBase64(Int16Array.from([0, 1]))).toBe("AAABAA==");
  });

  it("creates thirty 100 ms silence chunks", () => {
    expect(silenceBase64()).toHaveLength(30);
  });

  it("finds the final locally voiced 10 ms frame", () => {
    const samples = new Int16Array(2_400);
    samples.fill(2_000, 0, 480);

    expect(
      lastVoicedOffsetMilliseconds(pcm16ToBase64(samples)),
    ).toBe(20);
  });

  it("does not mark digital silence as speech", () => {
    expect(
      lastVoicedOffsetMilliseconds(
        pcm16ToBase64(new Int16Array(2_400)),
      ),
    ).toBeUndefined();
  });

  it("resamples a continuous 48 kHz stream near 24 kHz", () => {
    const resampler = new StreamingResampler(48_000);
    const first = resampler.push(new Float32Array(4_800));
    const second = resampler.push(new Float32Array(4_800));
    expect(first.length + second.length).toBeGreaterThanOrEqual(4_799);
    expect(first.length + second.length).toBeLessThanOrEqual(4_800);
  });
});
