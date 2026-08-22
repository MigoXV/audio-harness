import {
  graphNodeDimensions,
  graphNodePorts,
  layoutGraph,
  routingPortKey,
  type LayoutEdge,
  type LayoutNode,
  type PortOffsets,
  type RoutePoint,
} from "./graphLayout";

const NODE_CLEARANCE = 32;
const PORT_STUB = 36;
const BEND_PENALTY = 42;
const LANE_STEP = 12;
const OUTPUT_LANE_STEP = 18;

export interface AudioGraphLayout<NodeType> { nodes: NodeType[]; outputOrder: string[] }

export function orthogonalPath(points: RoutePoint[], radius = 14): string {
  const unique = points.filter((point, index) => index === 0 || point.x !== points[index - 1].x || point.y !== points[index - 1].y);
  if (unique.length < 2) return "";
  let path = `M ${unique[0].x} ${unique[0].y}`;
  for (let index = 1; index < unique.length - 1; index += 1) {
    const previous = unique[index - 1]; const current = unique[index]; const next = unique[index + 1];
    const incoming = Math.abs(current.x - previous.x) + Math.abs(current.y - previous.y);
    const outgoing = Math.abs(next.x - current.x) + Math.abs(next.y - current.y);
    const corner = Math.min(radius, incoming / 2, outgoing / 2);
    const before = {
      x: current.x + Math.sign(previous.x - current.x) * Math.min(corner, Math.abs(previous.x - current.x)),
      y: current.y + Math.sign(previous.y - current.y) * Math.min(corner, Math.abs(previous.y - current.y)),
    };
    const after = {
      x: current.x + Math.sign(next.x - current.x) * Math.min(corner, Math.abs(next.x - current.x)),
      y: current.y + Math.sign(next.y - current.y) * Math.min(corner, Math.abs(next.y - current.y)),
    };
    path += ` L ${before.x} ${before.y} Q ${current.x} ${current.y} ${after.x} ${after.y}`;
  }
  const last = unique[unique.length - 1];
  return `${path} L ${last.x} ${last.y}`;
}

interface Rect { left: number; right: number; top: number; bottom: number }
interface Segment { a: RoutePoint; b: RoutePoint }

export async function layoutAudioGraph<NodeType extends LayoutNode>(
  nodes: NodeType[],
  edges: LayoutEdge[],
  offsets: PortOffsets,
): Promise<AudioGraphLayout<NodeType>> {
  const layoutOptions = { graphId: "audio-workflow", firstNodeIds: ["$input"], lastNodeIds: ["$outputs"] };
  const first = await layoutGraph(nodes, edges, offsets, layoutOptions);
  const sourceYByOutput = new Map<string, number>();
  edges.filter((edge) => edge.target === "$outputs" && edge.targetHandle).forEach((edge) => {
    const source = first.find((node) => node.id === edge.source);
    const sourceOffset = offsets.get(routingPortKey(edge.source, "output", edge.sourceHandle));
    if (source && sourceOffset) sourceYByOutput.set(edge.targetHandle!, source.position.y + sourceOffset.y);
  });
  const outputNode = nodes.find((node) => node.id === "$outputs");
  const outputOrder = outputNode ? orderOutputPorts(graphNodePorts(outputNode, "input", edges).map((name) => ({ name })), sourceYByOutput).map(({ name }) => name) : [];
  const outputSlots = outputNode
    ? graphNodePorts(outputNode, "input", edges).flatMap((handle) => {
      const offset = offsets.get(routingPortKey(outputNode.id, "input", handle));
      return offset ? [offset] : [];
    }).sort((left, right) => left.y - right.y)
    : [];
  const orderedOffsets = new Map(offsets);
  outputOrder.forEach((handle, index) => {
    const slot = outputSlots[index];
    if (slot) orderedOffsets.set(routingPortKey("$outputs", "input", handle), slot);
  });
  const layouted = await layoutGraph(nodes, edges, orderedOffsets, layoutOptions);
  return { nodes: layouted, outputOrder };
}

function rectFor(node: LayoutNode): Rect {
  const size = graphNodeDimensions(node);
  return { left: node.position.x - NODE_CLEARANCE, right: node.position.x + size.width + NODE_CLEARANCE, top: node.position.y - NODE_CLEARANCE, bottom: node.position.y + size.height + NODE_CLEARANCE };
}

function handleAnchor(node: LayoutNode, handle: string | null | undefined, direction: "input" | "output", offsets: PortOffsets): RoutePoint | undefined {
  const offset = offsets.get(routingPortKey(node.id, direction, handle));
  if (!offset) return undefined;
  return { x: node.position.x + offset.x, y: node.position.y + offset.y };
}

function inside(point: RoutePoint, rect: Rect): boolean {
  const epsilon = 0.01;
  return point.x > rect.left + epsilon && point.x < rect.right - epsilon && point.y > rect.top + epsilon && point.y < rect.bottom - epsilon;
}

function segmentBlocked(a: RoutePoint, b: RoutePoint, obstacles: Rect[]): boolean {
  const epsilon = 0.01;
  return obstacles.some((rect) => {
    if (Math.abs(a.y - b.y) < epsilon) {
      if (a.y <= rect.top + epsilon || a.y >= rect.bottom - epsilon) return false;
      return Math.max(Math.min(a.x, b.x), rect.left) < Math.min(Math.max(a.x, b.x), rect.right) - epsilon;
    }
    if (Math.abs(a.x - b.x) < epsilon) {
      if (a.x <= rect.left + epsilon || a.x >= rect.right - epsilon) return false;
      return Math.max(Math.min(a.y, b.y), rect.top) < Math.min(Math.max(a.y, b.y), rect.bottom) - epsilon;
    }
    return true;
  });
}

function overlapLength(left: Segment, right: Segment): number {
  const horizontal = left.a.y === left.b.y && right.a.y === right.b.y && left.a.y === right.a.y;
  if (horizontal) return Math.max(0, Math.min(Math.max(left.a.x, left.b.x), Math.max(right.a.x, right.b.x)) - Math.max(Math.min(left.a.x, left.b.x), Math.min(right.a.x, right.b.x)));
  const vertical = left.a.x === left.b.x && right.a.x === right.b.x && left.a.x === right.a.x;
  if (vertical) return Math.max(0, Math.min(Math.max(left.a.y, left.b.y), Math.max(right.a.y, right.b.y)) - Math.max(Math.min(left.a.y, left.b.y), Math.min(right.a.y, right.b.y)));
  return 0;
}

function segmentsCross(left: Segment, right: Segment): boolean {
  const leftHorizontal = left.a.y === left.b.y;
  const rightHorizontal = right.a.y === right.b.y;
  if (leftHorizontal === rightHorizontal) return false;
  const horizontal = leftHorizontal ? left : right;
  const vertical = leftHorizontal ? right : left;
  return vertical.a.x > Math.min(horizontal.a.x, horizontal.b.x) && vertical.a.x < Math.max(horizontal.a.x, horizontal.b.x)
    && horizontal.a.y > Math.min(vertical.a.y, vertical.b.y) && horizontal.a.y < Math.max(vertical.a.y, vertical.b.y);
}

function simplify(points: RoutePoint[]): RoutePoint[] {
  const unique = points.filter((point, index) => index === 0 || point.x !== points[index - 1].x || point.y !== points[index - 1].y);
  return unique.filter((point, index) => {
    if (index === 0 || index === unique.length - 1) return true;
    const previous = unique[index - 1]; const next = unique[index + 1];
    return !((previous.x === point.x && point.x === next.x) || (previous.y === point.y && point.y === next.y));
  });
}

function findRoute(start: RoutePoint, end: RoutePoint, obstacles: Rect[], occupied: Segment[]): RoutePoint[] {
  const xs = new Set<number>([start.x, end.x]);
  const ys = new Set<number>([start.y, end.y]);
  obstacles.forEach((rect) => {
    [0, LANE_STEP, LANE_STEP * 2].forEach((offset) => {
      xs.add(rect.left - offset); xs.add(rect.right + offset); ys.add(rect.top - offset); ys.add(rect.bottom + offset);
    });
  });
  if (obstacles.length) {
    xs.add(Math.min(...obstacles.map((rect) => rect.left)) - 50); xs.add(Math.max(...obstacles.map((rect) => rect.right)) + 50);
    ys.add(Math.min(...obstacles.map((rect) => rect.top)) - 50); ys.add(Math.max(...obstacles.map((rect) => rect.bottom)) + 50);
  }
  const sortedX = [...xs].sort((a, b) => a - b);
  const sortedY = [...ys].sort((a, b) => a - b);
  const points: RoutePoint[] = [];
  const pointIndex = new Map<string, number>();
  sortedX.forEach((x) => sortedY.forEach((y) => {
    const point = { x, y };
    if (obstacles.some((rect) => inside(point, rect))) return;
    pointIndex.set(`${x}:${y}`, points.length); points.push(point);
  }));
  const neighbors: Array<Array<{ index: number; direction: "h" | "v"; distance: number }>> = points.map(() => []);
  const connectLine = (indices: number[], direction: "h" | "v") => {
    for (let index = 1; index < indices.length; index += 1) {
      const left = indices[index - 1]; const right = indices[index]; const a = points[left]; const b = points[right];
      if (segmentBlocked(a, b, obstacles)) continue;
      const distance = Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
      neighbors[left].push({ index: right, direction, distance }); neighbors[right].push({ index: left, direction, distance });
    }
  };
  sortedY.forEach((y) => connectLine(sortedX.flatMap((x) => { const index = pointIndex.get(`${x}:${y}`); return index == null ? [] : [index]; }), "h"));
  sortedX.forEach((x) => connectLine(sortedY.flatMap((y) => { const index = pointIndex.get(`${x}:${y}`); return index == null ? [] : [index]; }), "v"));
  const startIndex = pointIndex.get(`${start.x}:${start.y}`); const endIndex = pointIndex.get(`${end.x}:${end.y}`);
  if (startIndex == null || endIndex == null) return [start, end];
  type State = { point: number; direction: "h" | "v" | "start"; cost: number; previous?: string };
  const states = new Map<string, State>();
  const queue: State[] = [{ point: startIndex, direction: "start", cost: 0 }];
  states.set(`${startIndex}:start`, queue[0]);
  let winner: State | undefined;
  while (queue.length) {
    queue.sort((a, b) => a.cost - b.cost || a.point - b.point);
    const current = queue.shift()!;
    if (states.get(`${current.point}:${current.direction}`)?.cost !== current.cost) continue;
    if (current.point === endIndex) { winner = current; break; }
    neighbors[current.point].forEach((next) => {
      const segment = { a: points[current.point], b: points[next.index] };
      const overlap = occupied.reduce((sum, item) => sum + overlapLength(segment, item), 0);
      const crossings = occupied.filter((item) => segmentsCross(segment, item)).length;
      const bend = current.direction !== "start" && current.direction !== next.direction ? BEND_PENALTY : 0;
      const cost = current.cost + next.distance + bend + overlap * 24 + crossings * 120;
      const key = `${next.index}:${next.direction}`;
      if ((states.get(key)?.cost ?? Number.POSITIVE_INFINITY) <= cost) return;
      const state = { point: next.index, direction: next.direction, cost, previous: `${current.point}:${current.direction}` } satisfies State;
      states.set(key, state); queue.push(state);
    });
  }
  if (!winner) return [start, { x: start.x, y: end.y }, end];
  const path: RoutePoint[] = [];
  let current: State | undefined = winner;
  while (current) { path.push(points[current.point]); current = current.previous ? states.get(current.previous) : undefined; }
  return simplify(path.reverse());
}

export function routeAudioGraph(nodes: LayoutNode[], edges: LayoutEdge[], offsets: PortOffsets): Map<string, RoutePoint[]> {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const rects = new Map(nodes.map((node) => [node.id, rectFor(node)]));
  const occupied: Segment[] = [];
  const routes = new Map<string, RoutePoint[]>();
  const outputEdges = edges
    .filter((edge) => edge.target === "$outputs")
    .sort((left, right) => {
      const leftNode = byId.get(left.target); const rightNode = byId.get(right.target);
      const leftPoint = leftNode ? handleAnchor(leftNode, left.targetHandle, "input", offsets) : undefined;
      const rightPoint = rightNode ? handleAnchor(rightNode, right.targetHandle, "input", offsets) : undefined;
      return (leftPoint?.y ?? 0) - (rightPoint?.y ?? 0) || left.id.localeCompare(right.id);
    });
  const outputLane = new Map(outputEdges.map((edge, index) => [edge.id, index]));
  const orderedEdges = [...edges.filter((edge) => edge.target !== "$outputs"), ...outputEdges];
  orderedEdges.forEach((edge) => {
    const source = byId.get(edge.source); const target = byId.get(edge.target);
    if (!source || !target) return;
    const sourcePoint = handleAnchor(source, edge.sourceHandle, "output", offsets);
    const targetPoint = handleAnchor(target, edge.targetHandle, "input", offsets);
    if (!sourcePoint || !targetPoint) return;
    const obstacles = [...rects.entries()].filter(([id]) => id !== edge.source && id !== edge.target).map(([, rect]) => rect);
    const preferredSourceStub = { x: sourcePoint.x + PORT_STUB, y: sourcePoint.y };
    const sourceStub = obstacles.some((rect) => inside(preferredSourceStub, rect)) ? sourcePoint : preferredSourceStub;
    const lane = outputLane.get(edge.id);
    const targetStub = {
      x: lane == null
        ? targetPoint.x - PORT_STUB
        : target.position.x - NODE_CLEARANCE - PORT_STUB - lane * OUTPUT_LANE_STEP,
      y: targetPoint.y,
    };
    if (obstacles.some((rect) => inside(targetStub, rect))) targetStub.x = targetPoint.x;
    const route = simplify([sourcePoint, ...findRoute(sourceStub, targetStub, obstacles, occupied), targetPoint]);
    routes.set(edge.id, route);
    for (let index = 1; index < route.length; index += 1) occupied.push({ a: route[index - 1], b: route[index] });
  });
  return routes;
}

export function orderOutputPorts<Port extends { name: string }>(ports: readonly Port[], sourceYByOutput: ReadonlyMap<string, number>): Port[] {
  const originalOrder = new Map(ports.map((port, index) => [port.name, index]));
  return [...ports].sort((left, right) => {
    const leftY = sourceYByOutput.get(left.name); const rightY = sourceYByOutput.get(right.name);
    if (leftY == null && rightY == null) return (originalOrder.get(left.name) ?? 0) - (originalOrder.get(right.name) ?? 0);
    if (leftY == null) return 1;
    if (rightY == null) return -1;
    return leftY - rightY || (originalOrder.get(left.name) ?? 0) - (originalOrder.get(right.name) ?? 0);
  });
}

export function hasNodeCollisions(nodes: LayoutNode[], margin = 40): boolean {
  return nodes.some((node, index) => {
    const size = graphNodeDimensions(node);
    return nodes.slice(index + 1).some((other) => {
      const otherSize = graphNodeDimensions(other);
      return node.position.x < other.position.x + otherSize.width + margin && node.position.x + size.width + margin > other.position.x
        && node.position.y < other.position.y + otherSize.height + margin && node.position.y + size.height + margin > other.position.y;
    });
  });
}

export function needsAutoLayout(_nodes: LayoutNode[], requiredNodeIds: string[], positionedNodeIds: ReadonlySet<string>): boolean {
  return requiredNodeIds.some((nodeId) => !positionedNodeIds.has(nodeId));
}

export function routeIntersectsNode(route: RoutePoint[], node: LayoutNode, clearance = 0): boolean {
  const size = graphNodeDimensions(node);
  const rect = { left: node.position.x - clearance, right: node.position.x + size.width + clearance, top: node.position.y - clearance, bottom: node.position.y + size.height + clearance };
  return route.slice(1).some((point, index) => segmentBlocked(route[index], point, [rect]));
}
