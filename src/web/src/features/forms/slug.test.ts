import { describe, expect, it } from "vitest";

import { normalizeSlug, SLUG_MAX_LENGTH } from "./slug";

describe("normalizeSlug", () => {
  it("normalizes latin names into kebab-case", () => {
    expect(normalizeSlug("  Meeting ASR / Primary  ")).toBe("meeting-asr-primary");
    expect(normalizeSlug("Already---Clean")).toBe("already-clean");
  });

  it("does not transliterate Chinese names", () => {
    expect(normalizeSlug("客服助手")).toBe("");
    expect(normalizeSlug("客服 Support Agent")).toBe("support-agent");
  });

  it("respects the backend length limit without leaving a trailing separator", () => {
    const value = normalizeSlug(`${"a".repeat(SLUG_MAX_LENGTH - 1)} extra`);
    expect(value.length).toBeLessThanOrEqual(SLUG_MAX_LENGTH);
    expect(value).toMatch(/^[a-z0-9]+(?:-[a-z0-9]+)*$/);
  });
});
