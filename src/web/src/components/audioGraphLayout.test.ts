import type { Edge, Node } from "@xyflow/react";
import { describe, expect, it } from "vitest";

import { hasNodeCollisions, layoutAudioGraph, needsAutoLayout, orderOutputPorts, orthogonalPath, routeAudioGraph, routeIntersectsNode } from "./audioGraphLayout";
import { routingPortKey, type PortOffsets } from "./graphLayout";

function node(id: string, x = 0, y = 0, height = 160): Node {
  return { id, position: { x, y }, data: {}, measured: { width: 244, height } };
}

function edge(id: string, source: string, target: string, sourceHandle = "out", targetHandle = "in"): Edge {
  return { id, source, target, sourceHandle, targetHandle };
}

function measuredPorts(nodes: Node[], edges: Edge[]): PortOffsets {
  const result = new Map<string, { x: number; y: number; width: number; height: number }>();
  nodes.forEach((item) => {
    const inputs = [...new Set(edges.filter((candidate) => candidate.target === item.id).map((candidate) => candidate.targetHandle ?? "in"))];
    const outputs = [...new Set(edges.filter((candidate) => candidate.source === item.id).map((candidate) => candidate.sourceHandle ?? "out"))];
    inputs.forEach((handle, index) => result.set(routingPortKey(item.id, "input", handle), { x: -12, y: 74 + index * 30, width: 12, height: 12 }));
    outputs.forEach((handle, index) => result.set(routingPortKey(item.id, "output", handle), { x: 256, y: 74 + index * 30, width: 12, height: 12 }));
  });
  return result;
}

describe("audio graph layout", () => {
  it("lays out a branched graph from input to output without collisions", async () => {
    const nodes = [node("$input", 0, 0, 120), node("vad", 0, 0, 240), node("asr", 0, 0, 160), node("$outputs", 0, 0, 260)];
    const edges = [
      edge("input-vad", "$input", "vad"),
      edge("vad-asr", "vad", "asr"),
      edge("vad-event", "vad", "$outputs"),
      edge("asr-output", "asr", "$outputs"),
    ];

    const { nodes: layouted } = await layoutAudioGraph(nodes, edges, measuredPorts(nodes, edges));
    const positions = Object.fromEntries(layouted.map((item) => [item.id, item.position]));

    expect(positions.$input.x).toBeLessThan(positions.vad.x);
    expect(positions.vad.x).toBeLessThan(positions.asr.x);
    expect(positions.asr.x).toBeLessThan(positions.$outputs.x);
    expect(hasNodeCollisions(layouted)).toBe(false);
    expect(nodes.every((item) => item.position.x === 0 && item.position.y === 0)).toBe(true);
  });

  it("keeps output fan-in edges readable and left-to-right", async () => {
    const nodes = [
      node("$input", 0, 0, 120),
      node("vad", 0, 0, 240),
      node("semantic", 0, 0, 200),
      node("asr", 0, 0, 160),
      node("preview", 0, 0, 160),
      node("$outputs", 0, 0, 500),
    ];
    const edges = [
      edge("input-vad", "$input", "vad"),
      edge("vad-semantic", "vad", "semantic"),
      edge("semantic-asr", "semantic", "asr"),
      edge("input-preview", "$input", "preview"),
      edge("vad-event", "vad", "$outputs"),
      edge("semantic-event", "semantic", "$outputs"),
      edge("asr-output", "asr", "$outputs"),
      edge("preview-output", "preview", "$outputs"),
    ];

    const { nodes: layouted } = await layoutAudioGraph(nodes, edges, measuredPorts(nodes, edges));
    const positions = Object.fromEntries(layouted.map((item) => [item.id, item.position]));

    expect(positions.$input.x).toBeLessThan(positions.vad.x);
    expect(positions.vad.x).toBeLessThan(positions.semantic.x);
    expect(positions.semantic.x).toBeLessThan(positions.asr.x);
    expect(positions.$outputs.x).toBeGreaterThan(positions.asr.x + 244);
  });

  it("orders connected output ports by their source lanes", () => {
    const ports = [{ name: "primary" }, { name: "interim" }, { name: "stopped" }, { name: "unused" }];
    const ordered = orderOutputPorts(ports, new Map([
      ["interim", 500],
      ["primary", 100],
      ["stopped", 300],
    ]));

    expect(ordered.map((port) => port.name)).toEqual(["primary", "stopped", "interim", "unused"]);
  });

  it("only auto-arranges missing persisted layouts", () => {
    const separated = [node("a", 0, 0), node("b", 400, 0)];
    expect(needsAutoLayout(separated, ["a", "b"], new Set(["a"]))).toBe(true);
    expect(needsAutoLayout(separated, ["a", "b"], new Set(["a", "b"]))).toBe(false);
    const overlapping = [node("a", 0, 0), node("b", 260, 0)];
    expect(hasNodeCollisions(overlapping)).toBe(true);
    expect(needsAutoLayout(overlapping, ["a", "b"], new Set(["a", "b"]))).toBe(false);
  });

  it("routes long edges around intermediate nodes", () => {
    const nodes = [node("source", 0, 100), node("obstacle", 320, 80, 220), node("target", 700, 100)];
    const edges = [edge("source-target", "source", "target")];
    const offsets = measuredPorts(nodes, edges);
    const route = routeAudioGraph(nodes, edges, offsets).get("source-target");

    expect(route).toBeDefined();
    expect(route![0]).toEqual({ x: 256, y: 174 });
    expect(route!.at(-1)).toEqual({ x: 688, y: 174 });
    expect(routeIntersectsNode(route!, nodes[1], 1)).toBe(false);
    expect(route!.every((point, index) => index === 0 || point.x === route![index - 1].x || point.y === route![index - 1].y)).toBe(true);
  });

  it("does not invent a route before both real handles are measured", () => {
    const nodes = [node("source", 0, 0), node("target", 500, 0)];
    const edges = [edge("source-target", "source", "target")];
    const offsets = new Map(measuredPorts(nodes, edges));
    offsets.delete(routingPortKey("target", "input", "in"));

    expect(routeAudioGraph(nodes, edges, offsets).has("source-target")).toBe(false);
  });

  it("renders orthogonal bends with a large adaptive radius", () => {
    expect(orthogonalPath([{ x: 0, y: 0 }, { x: 40, y: 0 }, { x: 40, y: 40 }, { x: 100, y: 40 }]))
      .toBe("M 0 0 L 26 0 Q 40 0 40 14 L 40 26 Q 40 40 54 40 L 100 40");
  });

  it("produces deterministic separated routes for a dense output fan-in", async () => {
    const nodes = [node("a", 0, 0), node("b", 0, 260), node("c", 430, 120), node("$outputs", 850, 60, 420)];
    const edges = [
      edge("a-out", "a", "$outputs", "out", "one"),
      edge("b-out", "b", "$outputs", "out", "two"),
      edge("c-out", "c", "$outputs", "out", "three"),
    ];
    const offsets = measuredPorts(nodes, edges);
    const first = await layoutAudioGraph(nodes, edges, offsets);
    const second = await layoutAudioGraph(nodes, edges, offsets);

    expect(first.nodes.map((item) => item.position)).toEqual(second.nodes.map((item) => item.position));
    const firstRoutes = routeAudioGraph(first.nodes, edges, offsets);
    const secondRoutes = routeAudioGraph(second.nodes, edges, offsets);
    expect([...firstRoutes.values()]).toEqual([...secondRoutes.values()]);
    expect(new Set([...firstRoutes.values()].map((route) => JSON.stringify(route))).size).toBe(edges.length);
  });

  it("keeps meeting-style workflow routes out of unrelated nodes", async () => {
    const nodes = [
      node("$input", 0, 0, 120), node("preview-vad", 0, 0, 220), node("preview-asr"),
      node("vad", 0, 0, 240), node("semantic", 0, 0, 220), node("embedding"), node("search"),
      node("diarization", 0, 0, 220), node("asr", 0, 0, 180), node("speaker-asr", 0, 0, 180),
      node("clustering", 0, 0, 180), node("$outputs", 0, 0, 440),
    ];
    const edges = [
      edge("input-preview", "$input", "preview-vad", "audio", "audio"),
      edge("preview-asr", "preview-vad", "preview-asr", "segments", "segments"),
      edge("preview-output", "preview-asr", "$outputs", "transcripts", "interim"),
      edge("input-vad", "$input", "vad", "audio", "audio"),
      edge("vad-semantic", "vad", "semantic", "segments", "segments"),
      edge("semantic-asr", "semantic", "asr", "segments", "segments"),
      edge("semantic-embedding", "semantic", "embedding", "segments", "segments"),
      edge("embedding-search", "embedding", "search", "embeddings", "embeddings"),
      edge("semantic-diarization", "semantic", "diarization", "segments", "segments"),
      edge("diarization-asr", "diarization", "speaker-asr", "segments", "segments"),
      edge("diarization-cluster", "diarization", "clustering", "local", "segments"),
      edge("asr-output", "asr", "$outputs", "transcripts", "primary"),
      edge("speaker-output", "speaker-asr", "$outputs", "transcripts", "speaker"),
      edge("search-output", "search", "$outputs", "results", "search"),
      edge("cluster-output", "clustering", "$outputs", "results", "cluster"),
    ];
    const offsets = measuredPorts(nodes, edges);
    const result = await layoutAudioGraph(nodes, edges, offsets);
    const routes = routeAudioGraph(result.nodes, edges, offsets);
    const positions = new Map(result.nodes.map((item) => [item.id, item.position]));

    expect(positions.get("preview-vad")!.x).toBeCloseTo(positions.get("vad")!.x);
    expect(positions.get("preview-asr")!.x).toBeCloseTo(positions.get("semantic")!.x);
    expect(positions.get("embedding")!.x).toBeCloseTo(positions.get("diarization")!.x);
    expect(positions.get("search")!.x).toBeCloseTo(positions.get("speaker-asr")!.x);
    expect(positions.get("search")!.x).toBeCloseTo(positions.get("clustering")!.x);
    expect(positions.get("$outputs")!.x).toBeGreaterThan(positions.get("search")!.x);

    const outputRoutes = edges.filter((item) => item.target === "$outputs").map((item) => routes.get(item.id)!);
    expect(new Set(outputRoutes.map((route) => route.at(-2)!.x)).size).toBe(outputRoutes.length);

    edges.forEach((item) => {
      const route = routes.get(item.id)!;
      const source = result.nodes.find((candidate) => candidate.id === item.source)!;
      const target = result.nodes.find((candidate) => candidate.id === item.target)!;
      const sourceOffset = offsets.get(routingPortKey(item.source, "output", item.sourceHandle))!;
      const targetOffset = offsets.get(routingPortKey(item.target, "input", item.targetHandle))!;
      expect(route[0]).toEqual({ x: source.position.x + sourceOffset.x, y: source.position.y + sourceOffset.y });
      expect(route.at(-1)).toEqual({ x: target.position.x + targetOffset.x, y: target.position.y + targetOffset.y });
      expect(route.some((point, index) => index > 0 && point.x === route[index - 1].x && point.y === route[index - 1].y)).toBe(false);
      expect(route.every((point, index) => index === 0 || point.x === route[index - 1].x || point.y === route[index - 1].y)).toBe(true);
      result.nodes.filter((candidate) => candidate.id !== item.source && candidate.id !== item.target).forEach((candidate) => {
        expect(routeIntersectsNode(route, candidate, 1), `${item.id} crosses ${candidate.id}`).toBe(false);
      });
    });
  });
});
