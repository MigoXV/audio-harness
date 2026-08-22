import type { Edge, Node } from "@xyflow/react";
import type { ELK as ElkApi, ElkNode } from "elkjs/lib/elk-api";

const DEFAULT_NODE_WIDTH = 244;
const DEFAULT_NODE_HEIGHT = 160;

type PortDescription = { id?: string; name?: string };

export type LayoutNode = Pick<Node, "id" | "position" | "width" | "height" | "measured" | "data">;
export type LayoutEdge = Pick<Edge, "id" | "source" | "target" | "sourceHandle" | "targetHandle">;
export interface RoutePoint { x: number; y: number }
export interface PortOffset extends RoutePoint { width?: number; height?: number }
export type PortOffsets = ReadonlyMap<string, PortOffset>;
export interface GraphHandleBounds {
  source?: Array<{ id?: string | null; x: number; y: number; width: number; height: number }> | null;
  target?: Array<{ id?: string | null; x: number; y: number; width: number; height: number }> | null;
}

export interface GraphLayoutOptions {
  graphId?: string;
  firstNodeIds?: readonly string[];
  lastNodeIds?: readonly string[];
}

let elkInstance: Promise<ElkApi> | null = null;

async function getElk(): Promise<ElkApi> {
  elkInstance ??= import("elkjs/lib/elk.bundled.js").then(({ default: ELK }) => new ELK());
  return elkInstance;
}

export function graphNodeDimensions(node: LayoutNode): { width: number; height: number } {
  return {
    width: node.measured?.width ?? node.width ?? DEFAULT_NODE_WIDTH,
    height: node.measured?.height ?? node.height ?? DEFAULT_NODE_HEIGHT,
  };
}

export function graphNodePorts(node: LayoutNode, direction: "input" | "output", edges: LayoutEdge[]): string[] {
  const data = node.data as { inputs?: PortDescription[]; outputs?: PortDescription[] } | undefined;
  const described = direction === "input" ? data?.inputs : data?.outputs;
  if (described?.length) return described.flatMap((port) => port.name ?? port.id ?? []);
  const values = edges.flatMap((edge) => {
    const matches = direction === "input" ? edge.target === node.id : edge.source === node.id;
    const handle = direction === "input" ? edge.targetHandle : edge.sourceHandle;
    return matches && handle ? [handle] : [];
  });
  return [...new Set(values)];
}

export function routingPortKey(nodeId: string, direction: "input" | "output", handle: string | null | undefined): string {
  return `${nodeId}::${direction}::${handle ?? "default"}`;
}

export function collectGraphPortOffsets(
  nodeIds: readonly string[],
  getHandleBounds: (nodeId: string) => GraphHandleBounds | undefined,
): PortOffsets {
  const offsets = new Map<string, PortOffset>();
  nodeIds.forEach((nodeId) => {
    const bounds = getHandleBounds(nodeId);
    bounds?.target?.forEach((handle) => offsets.set(
      routingPortKey(nodeId, "input", handle.id),
      { x: handle.x + handle.width / 2, y: handle.y + handle.height / 2, width: handle.width, height: handle.height },
    ));
    bounds?.source?.forEach((handle) => offsets.set(
      routingPortKey(nodeId, "output", handle.id),
      { x: handle.x + handle.width / 2, y: handle.y + handle.height / 2, width: handle.width, height: handle.height },
    ));
  });
  return offsets;
}

function elkPort(node: LayoutNode, direction: "input" | "output", handle: string, offsets: PortOffsets) {
  const offset = offsets.get(routingPortKey(node.id, direction, handle));
  if (!offset) throw new Error(`端口 ${node.id}.${handle} 尚未完成测量`);
  const width = offset.width ?? 1;
  const height = offset.height ?? 1;
  return {
    id: routingPortKey(node.id, direction, handle),
    x: offset.x - width / 2,
    y: offset.y - height / 2,
    width,
    height,
    layoutOptions: { "elk.port.side": direction === "input" ? "WEST" : "EAST" },
  };
}

export async function layoutGraph<NodeType extends LayoutNode>(
  nodes: NodeType[],
  edges: LayoutEdge[],
  offsets: PortOffsets,
  options: GraphLayoutOptions = {},
): Promise<NodeType[]> {
  const firstNodeIds = new Set(options.firstNodeIds ?? []);
  const lastNodeIds = new Set(options.lastNodeIds ?? []);
  const graph: ElkNode = {
    id: options.graphId ?? "graph",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.spacing.nodeNode": "96",
      "elk.spacing.edgeEdge": "18",
      "elk.spacing.edgeNode": "40",
      "elk.layered.spacing.nodeNodeBetweenLayers": "180",
      "elk.layered.spacing.edgeNodeBetweenLayers": "48",
      "elk.layered.layering.strategy": "LONGEST_PATH_SOURCE",
      "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      "elk.padding": "[top=48,left=48,bottom=48,right=48]",
    },
    children: nodes.map((node) => {
      const size = graphNodeDimensions(node);
      const inputs = graphNodePorts(node, "input", edges);
      const outputs = graphNodePorts(node, "output", edges);
      return {
        id: node.id,
        width: size.width,
        height: size.height,
        layoutOptions: {
          "elk.portConstraints": "FIXED_POS",
          ...(firstNodeIds.has(node.id) ? { "elk.layered.layering.layerConstraint": "FIRST" } : {}),
          ...(lastNodeIds.has(node.id) ? { "elk.layered.layering.layerConstraint": "LAST" } : {}),
        },
        ports: [
          ...inputs.map((handle) => elkPort(node, "input", handle, offsets)),
          ...outputs.map((handle) => elkPort(node, "output", handle, offsets)),
        ],
      };
    }),
    edges: edges.map((edge) => ({
      id: edge.id,
      sources: [routingPortKey(edge.source, "output", edge.sourceHandle)],
      targets: [routingPortKey(edge.target, "input", edge.targetHandle)],
    })),
  };
  const result = await (await getElk()).layout(graph);
  const positions = new Map((result.children ?? []).map((node) => [node.id, node]));
  return nodes.map((node) => {
    const position = positions.get(node.id);
    if (!position || !Number.isFinite(position.x) || !Number.isFinite(position.y)) throw new Error(`ELK 没有返回节点 ${node.id} 的有效位置`);
    return { ...node, position: { x: position.x!, y: position.y! } };
  });
}
