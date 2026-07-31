import { describe, expect, it } from "vitest";

import { wavExcerpt } from "./wav.js";

function silentWav(seconds: number, sampleRate = 16_000): Buffer {
  const dataSize = seconds * sampleRate * 2;
  const wav = Buffer.alloc(44 + dataSize);
  wav.write("RIFF", 0, "ascii");
  wav.writeUInt32LE(wav.length - 8, 4);
  wav.write("WAVE", 8, "ascii");
  wav.write("fmt ", 12, "ascii");
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(sampleRate, 24);
  wav.writeUInt32LE(sampleRate * 2, 28);
  wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34);
  wav.write("data", 36, "ascii");
  wav.writeUInt32LE(dataSize, 40);
  return wav;
}

describe("wavExcerpt", () => {
  it("returns at most the first 30 seconds and repairs sizes", () => {
    const excerpt = wavExcerpt(silentWav(35));

    expect(excerpt.length).toBe(44 + 30 * 16_000 * 2);
    expect(excerpt.readUInt32LE(4)).toBe(excerpt.length - 8);
    expect(excerpt.readUInt32LE(40)).toBe(30 * 16_000 * 2);
  });

  it("rejects non-WAV data", () => {
    expect(() => wavExcerpt(Buffer.from("not wave"))).toThrow(
      "RIFF/WAVE",
    );
  });
});
