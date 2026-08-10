import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";

const DEFAULT_NODE_WIDTH = 244;
const DEFAULT_NODE_HEIGHT = 160;
const NODE_SEPARATION = 70;
const RANK_SEPARATION = 120;

type LayoutNode = Pick<Node, "id" | "position" | "width" | "height" | "measured">;
type LayoutEdge = Pick<Edge, "id" | "source" | "target">;

function dimensions(node: LayoutNode): { width: number; height: number } {
  return {
    width: node.measured?.width ?? node.width ?? DEFAULT_NODE_WIDTH,
    height: node.measured?.height ?? node.height ?? DEFAULT_NODE_HEIGHT,
  };
}

export function layoutAudioGraph<NodeType extends LayoutNode>(
  nodes: NodeType[],
  edges: LayoutEdge[],
): NodeType[] {
  const graph = new dagre.graphlib.Graph({ multigraph: true })
    .setDefaultEdgeLabel(() => ({}))
    .setGraph({
      rankdir: "LR",
      ranksep: RANK_SEPARATION,
      nodesep: NODE_SEPARATION,
      marginx: 40,
      marginy: 40,
      acyclicer: "greedy",
      ranker: "network-simplex",
    });

  nodes.forEach((node) => graph.setNode(node.id, dimensions(node)));
  edges.forEach((edge) => graph.setEdge(edge.source, edge.target, {}, edge.id));
  dagre.layout(graph);

  return nodes.map((node) => {
    const position = graph.node(node.id);
    const { width, height } = dimensions(node);
    if (!position || !Number.isFinite(position.x) || !Number.isFinite(position.y)) {
      throw new Error(`Dagre 没有返回节点 ${node.id} 的有效位置`);
    }
    return {
      ...node,
      position: {
        x: position.x - width / 2,
        y: position.y - height / 2,
      },
    };
  });
}

export function hasNodeCollisions(nodes: LayoutNode[], margin = 40): boolean {
  return nodes.some((node, index) => {
    const size = dimensions(node);
    return nodes.slice(index + 1).some((other) => {
      const otherSize = dimensions(other);
      return (
        node.position.x < other.position.x + otherSize.width + margin
        && node.position.x + size.width + margin > other.position.x
        && node.position.y < other.position.y + otherSize.height + margin
        && node.position.y + size.height + margin > other.position.y
      );
    });
  });
}

export function needsAutoLayout(
  nodes: LayoutNode[],
  requiredNodeIds: string[],
  positionedNodeIds: ReadonlySet<string>,
): boolean {
  return requiredNodeIds.some((nodeId) => !positionedNodeIds.has(nodeId)) || hasNodeCollisions(nodes);
}
