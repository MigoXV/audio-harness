import assert from "node:assert/strict";
import test from "node:test";

import { floatToPcm16, splitPcmChunks } from "../web/audio.js";

test("floatToPcm16 converts and clamps PCM samples", () => {
  const bytes = floatToPcm16(new Float32Array([-2, -1, 0, 1, 2]));
  assert.deepEqual([...new Int16Array(bytes.buffer)], [-32_768, -32_768, 0, 32_767, 32_767]);
});

test("splitPcmChunks preserves order and permits a final short chunk", () => {
  const chunks = splitPcmChunks(new Uint8Array([1, 2, 3, 4, 5]), 2);
  assert.deepEqual(chunks.map((chunk) => [...chunk]), [[1, 2], [3, 4], [5]]);
});
