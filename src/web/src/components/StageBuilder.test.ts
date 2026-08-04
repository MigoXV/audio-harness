import { describe, expect, it } from "vitest";

import { updateAllowedVoices } from "./StageBuilder";

describe("updateAllowedVoices", () => {
  it("uses the first allowed voice when the saved default is no longer valid", () => {
    expect(updateAllowedVoices({
      allowed_voices: ["old-voice"],
      default_voice: "old-voice ",
      language: "Chinese",
      decoder_chunk_size: 0,
    }, " anbi ")).toMatchObject({
      allowed_voices: ["anbi"],
      default_voice: "anbi",
    });
  });

  it("keeps a valid default voice", () => {
    expect(updateAllowedVoices({
      allowed_voices: ["anbi", "benny"],
      default_voice: "benny",
      language: "Chinese",
      decoder_chunk_size: 0,
    }, "anbi, benny")).toMatchObject({
      allowed_voices: ["anbi", "benny"],
      default_voice: "benny",
    });
  });
});
