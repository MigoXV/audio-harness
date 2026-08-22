import type { Edge, Node } from "@xyflow/react";
import { describe, expect, it } from "vitest";

import { layoutGraph, routingPortKey, type PortOffsets } from "./graphLayout";

function offsets(): PortOffsets {
  return new Map([
    [routingPortKey("entry", "output", "audio"), { x: 244, y: 80, width: 12, height: 12 }],
    [routingPortKey("agent", "input", "audio"), { x: 0, y: 60, width: 12, height: 12 }],
    [routingPortKey("agent", "output", "speech"), { x: 244, y: 100, width: 12, height: 12 }],
    [routingPortKey("exit", "input", "speech"), { x: 0, y: 80, width: 12, height: 12 }],
  ]);
}

describe("generic graph layout", () => {
  it("lays out arbitrary boundary ids and id-based port descriptions", async () => {
    const nodes: Node[] = [
      { id: "entry", position: { x: 0, y: 0 }, measured: { width: 244, height: 160 }, data: { inputs: [], outputs: [{ id: "audio" }] } },
      { id: "agent", position: { x: 0, y: 0 }, measured: { width: 244, height: 160 }, data: { inputs: [{ id: "audio" }], outputs: [{ id: "speech" }] } },
      { id: "exit", position: { x: 0, y: 0 }, measured: { width: 244, height: 160 }, data: { inputs: [{ id: "speech" }], outputs: [] } },
    ];
    const edges: Edge[] = [
      { id: "entry-agent", source: "entry", sourceHandle: "audio", target: "agent", targetHandle: "audio" },
      { id: "agent-exit", source: "agent", sourceHandle: "speech", target: "exit", targetHandle: "speech" },
    ];

    const layouted = await layoutGraph(nodes, edges, offsets(), { firstNodeIds: ["entry"], lastNodeIds: ["exit"] });
    const positions = Object.fromEntries(layouted.map((node) => [node.id, node.position]));

    expect(positions.entry.x).toBeLessThan(positions.agent.x);
    expect(positions.agent.x).toBeLessThan(positions.exit.x);
    expect(nodes.every((node) => node.position.x === 0)).toBe(true);
  });

  it("supports a layered main path with a cyclic delegation branch", async () => {
    const nodes: Node[] = [
      { id: "input", position: { x: 0, y: 0 }, measured: { width: 244, height: 160 }, data: { inputs: [], outputs: [{ id: "audio" }] } },
      { id: "workflow", position: { x: 0, y: 0 }, measured: { width: 244, height: 160 }, data: { inputs: [{ id: "audio" }], outputs: [{ id: "transcript" }] } },
      { id: "responses", position: { x: 0, y: 0 }, measured: { width: 244, height: 200 }, data: { inputs: [{ id: "transcript" }, { id: "result" }], outputs: [{ id: "text" }, { id: "delegate" }] } },
      { id: "backend", position: { x: 0, y: 0 }, measured: { width: 244, height: 160 }, data: { inputs: [{ id: "delegate" }], outputs: [{ id: "result" }] } },
      { id: "tts", position: { x: 0, y: 0 }, measured: { width: 244, height: 160 }, data: { inputs: [{ id: "text" }], outputs: [{ id: "speech" }] } },
      { id: "output", position: { x: 0, y: 0 }, measured: { width: 244, height: 160 }, data: { inputs: [{ id: "speech" }], outputs: [] } },
    ];
    const edges: Edge[] = [
      { id: "input-workflow", source: "input", sourceHandle: "audio", target: "workflow", targetHandle: "audio" },
      { id: "workflow-responses", source: "workflow", sourceHandle: "transcript", target: "responses", targetHandle: "transcript" },
      { id: "responses-tts", source: "responses", sourceHandle: "text", target: "tts", targetHandle: "text" },
      { id: "tts-output", source: "tts", sourceHandle: "speech", target: "output", targetHandle: "speech" },
      { id: "responses-backend", source: "responses", sourceHandle: "delegate", target: "backend", targetHandle: "delegate" },
      { id: "backend-responses", source: "backend", sourceHandle: "result", target: "responses", targetHandle: "result" },
    ];
    const portOffsets = new Map<string, { x: number; y: number; width: number; height: number }>();
    nodes.forEach((node) => {
      const data = node.data as { inputs: Array<{ id: string }>; outputs: Array<{ id: string }> };
      data.inputs.forEach((port, index) => portOffsets.set(routingPortKey(node.id, "input", port.id), { x: 0, y: 60 + index * 32, width: 12, height: 12 }));
      data.outputs.forEach((port, index) => portOffsets.set(routingPortKey(node.id, "output", port.id), { x: 244, y: 60 + index * 32, width: 12, height: 12 }));
    });

    const layouted = await layoutGraph(nodes, edges, portOffsets, { firstNodeIds: ["input"], lastNodeIds: ["output"] });
    const positions = Object.fromEntries(layouted.map((node) => [node.id, node.position]));

    expect(positions.input.x).toBeLessThan(positions.workflow.x);
    expect(positions.workflow.x).toBeLessThan(positions.responses.x);
    expect(positions.tts.x).toBeLessThan(positions.output.x);
    expect(Number.isFinite(positions.backend.x)).toBe(true);
    expect(Number.isFinite(positions.backend.y)).toBe(true);
  });
});
