import type { Edge, Node } from "@xyflow/react";
import { describe, expect, it } from "vitest";

import { hasNodeCollisions, layoutAudioGraph, needsAutoLayout } from "./audioGraphLayout";

function node(id: string, x = 0, y = 0, height = 160): Node {
  return { id, position: { x, y }, data: {}, measured: { width: 244, height } };
}

function edge(id: string, source: string, target: string): Edge {
  return { id, source, target };
}

describe("audio graph layout", () => {
  it("lays out a branched graph from input to output without collisions", () => {
    const nodes = [node("$input", 0, 0, 120), node("vad", 0, 0, 240), node("asr", 0, 0, 160), node("$outputs", 0, 0, 260)];
    const edges = [
      edge("input-vad", "$input", "vad"),
      edge("vad-asr", "vad", "asr"),
      edge("vad-event", "vad", "$outputs"),
      edge("asr-output", "asr", "$outputs"),
    ];

    const layouted = layoutAudioGraph(nodes, edges);
    const positions = Object.fromEntries(layouted.map((item) => [item.id, item.position]));

    expect(positions.$input.x).toBeLessThan(positions.vad.x);
    expect(positions.vad.x).toBeLessThan(positions.asr.x);
    expect(positions.asr.x).toBeLessThan(positions.$outputs.x);
    expect(hasNodeCollisions(layouted)).toBe(false);
    expect(nodes.every((item) => item.position.x === 0 && item.position.y === 0)).toBe(true);
  });

  it("detects missing and overlapping persisted layouts", () => {
    const separated = [node("a", 0, 0), node("b", 400, 0)];
    expect(needsAutoLayout(separated, ["a", "b"], new Set(["a"]))).toBe(true);
    expect(needsAutoLayout(separated, ["a", "b"], new Set(["a", "b"]))).toBe(false);
    expect(hasNodeCollisions([node("a", 0, 0), node("b", 260, 0)])).toBe(true);
  });
});
