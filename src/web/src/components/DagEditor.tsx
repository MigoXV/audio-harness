import {
  applyEdgeChanges,
  applyNodeChanges,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import { useMemo } from "react";

import type {
  EngineAsset,
  NodeTypeDefinition,
  PipelineDefinition,
  PipelineDiagnostic,
  PipelineNode,
} from "../api/types";
import {
  capabilityLabel,
  nodeCapability,
  nodeDisplayName,
} from "../model";

interface DagNodeData extends Record<string, unknown> {
  label: string;
  capability: string;
  engine: string;
  inputPorts: string[];
  outputPorts: string[];
  source: boolean;
  sink: boolean;
  invalid: boolean;
  configure: () => void;
}

function DagNode({ data, selected }: NodeProps<Node<DagNodeData>>) {
  return (
    <div
      className={`dag-node ${selected ? "is-selected" : ""} ${
        data.invalid ? "is-invalid" : ""
      }`}
    >
      {data.inputPorts.map((port, index) => (
        <Handle
          key={port}
          id={port}
          type="target"
          position={Position.Left}
          style={{
            top: `${((index + 1) / (data.inputPorts.length + 1)) * 100}%`,
          }}
          aria-label={`输入端口 ${port}`}
        />
      ))}
      <span>{data.source ? "输入" : data.sink ? "输出" : data.capability}</span>
      <strong>{data.label}</strong>
      <small>{data.engine}</small>
      {!data.source && !data.sink && (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            data.configure();
          }}
        >
          配置节点
        </button>
      )}
      {data.outputPorts.map((port, index) => (
        <Handle
          key={port}
          id={port}
          type="source"
          position={Position.Right}
          style={{
            top: `${((index + 1) / (data.outputPorts.length + 1)) * 100}%`,
          }}
          aria-label={`输出端口 ${port}`}
        />
      ))}
    </div>
  );
}

const nodeRenderers = { pipelineNode: DagNode };

function createsCycle(
  definition: PipelineDefinition,
  connection: Connection,
): boolean {
  if (!connection.source || !connection.target) {
    return true;
  }
  const outgoing = new Map<string, string[]>();
  for (const edge of definition.edges) {
    outgoing.set(edge.source_node, [
      ...(outgoing.get(edge.source_node) ?? []),
      edge.target_node,
    ]);
  }
  outgoing.set(connection.source, [
    ...(outgoing.get(connection.source) ?? []),
    connection.target,
  ]);
  const stack = [connection.target];
  const visited = new Set<string>();
  while (stack.length > 0) {
    const current = stack.pop()!;
    if (current === connection.source) {
      return true;
    }
    if (visited.has(current)) {
      continue;
    }
    visited.add(current);
    stack.push(...(outgoing.get(current) ?? []));
  }
  return false;
}

function nextIdentifier(prefix: string, existing: Iterable<string>): string {
  const taken = new Set(existing);
  let suffix = 1;
  while (taken.has(`${prefix}_${suffix}`)) {
    suffix += 1;
  }
  return `${prefix}_${suffix}`;
}

interface DagEditorProps {
  definition: PipelineDefinition;
  nodeTypes: NodeTypeDefinition[];
  engines: Map<string, EngineAsset>;
  diagnostics: PipelineDiagnostic[];
  saving: boolean;
  validating: boolean;
  activating: boolean;
  saved: boolean;
  valid: boolean;
  activationNeeded: boolean;
  onChange: (definition: PipelineDefinition) => void;
  onConfigureNode: (node: PipelineNode) => void;
  onSave: () => void;
  onValidate: () => void;
  onActivate: () => void;
  onClose: () => void;
}

export function DagEditor({
  definition,
  nodeTypes,
  engines,
  diagnostics,
  saving,
  validating,
  activating,
  saved,
  valid,
  activationNeeded,
  onChange,
  onConfigureNode,
  onSave,
  onValidate,
  onActivate,
  onClose,
}: DagEditorProps) {
  const typeByName = useMemo(
    () => new Map(nodeTypes.map((type) => [type.type, type])),
    [nodeTypes],
  );
  const invalidNodes = useMemo(
    () =>
      new Set(
        diagnostics
          .map((item) => item.node_id)
          .filter((value): value is string => value !== null),
      ),
    [diagnostics],
  );
  const nodes = useMemo<Node<DagNodeData>[]>(
    () =>
      definition.nodes.map((node) => {
        const type = typeByName.get(node.type);
        const engine = node.engine_id
          ? engines.get(node.engine_id) ?? null
          : null;
        return {
          id: node.id,
          type: "pipelineNode",
          position: node.position,
          deletable: !(type?.source || type?.sink),
          data: {
            label: nodeDisplayName(node, nodeTypes),
            capability: capabilityLabel(nodeCapability(node.type)),
            engine: type?.required_api
              ? engine?.name ?? "尚未选择引擎"
              : "内置运行时节点",
            inputPorts: Object.keys(type?.input_ports ?? {}),
            outputPorts: Object.keys(type?.output_ports ?? {}),
            source: type?.source ?? false,
            sink: type?.sink ?? false,
            invalid: invalidNodes.has(node.id),
            configure: () => onConfigureNode(node),
          },
        };
      }),
    [
      definition.nodes,
      engines,
      invalidNodes,
      nodeTypes,
      onConfigureNode,
      typeByName,
    ],
  );
  const edges = useMemo<Edge[]>(
    () =>
      definition.edges.map((edge) => ({
        id: edge.id,
        source: edge.source_node,
        sourceHandle: edge.source_port,
        target: edge.target_node,
        targetHandle: edge.target_port,
        type: "smoothstep",
      })),
    [definition.edges],
  );

  const changeNodes = (changes: NodeChange<Node<DagNodeData>>[]) => {
    const nextNodes = applyNodeChanges(changes, nodes);
    const remaining = new Set(nextNodes.map((node) => node.id));
    onChange({
      ...definition,
      nodes: definition.nodes
        .filter((node) => remaining.has(node.id))
        .map((node) => {
          const visual = nextNodes.find((item) => item.id === node.id);
          return visual
            ? { ...node, position: visual.position }
            : node;
        }),
      edges: definition.edges.filter(
        (edge) =>
          remaining.has(edge.source_node) &&
          remaining.has(edge.target_node),
      ),
    });
  };

  const changeEdges = (changes: EdgeChange[]) => {
    const nextEdges = applyEdgeChanges(changes, edges);
    const remaining = new Set(nextEdges.map((edge) => edge.id));
    onChange({
      ...definition,
      edges: definition.edges.filter((edge) => remaining.has(edge.id)),
    });
  };

  const connect = (connection: Connection) => {
    if (
      !connection.source ||
      !connection.target ||
      !connection.sourceHandle ||
      !connection.targetHandle ||
      createsCycle(definition, connection)
    ) {
      return;
    }
    const source = definition.nodes.find(
      (node) => node.id === connection.source,
    );
    const target = definition.nodes.find(
      (node) => node.id === connection.target,
    );
    const sourceType = source ? typeByName.get(source.type) : null;
    const targetType = target ? typeByName.get(target.type) : null;
    const output = sourceType?.output_ports[connection.sourceHandle];
    const input = targetType?.input_ports[connection.targetHandle];
    if (!output || !input || output.payload_type !== input.payload_type) {
      return;
    }
    const id = nextIdentifier(
      "edge",
      definition.edges.map((edge) => edge.id),
    );
    onChange({
      ...definition,
      edges: [
        ...definition.edges,
        {
          id,
          source_node: connection.source,
          source_port: connection.sourceHandle,
          target_node: connection.target,
          target_port: connection.targetHandle,
        },
      ],
    });
  };

  const addNode = (type: NodeTypeDefinition) => {
    const count = definition.nodes.filter(
      (node) => node.type === type.type,
    ).length;
    if (type.max_instances !== null && count >= type.max_instances) {
      return;
    }
    const id = nextIdentifier(
      type.type,
      definition.nodes.map((node) => node.id),
    );
    onChange({
      ...definition,
      nodes: [
        ...definition.nodes,
        {
          id,
          type: type.type,
          display_name: null,
          engine_id: null,
          config: {},
          failure_policy: type.default_failure_policy,
          position: {
            x: 360 + count * 40,
            y: 120 + definition.nodes.length * 34,
          },
        },
      ],
    });
  };

  return (
    <section className="dag-editor-shell" aria-label="处理链拓扑编辑器">
      <header className="dag-editor-heading">
        <div>
          <h1>编辑处理链拓扑</h1>
          <p>连接类型兼容的端口；服务端校验结果是最终依据。</p>
        </div>
        <button className="button button-quiet" type="button" onClick={onClose}>
          返回查看
        </button>
      </header>

      <div className="dag-editor-body">
        <aside className="dag-node-palette">
          <strong>添加节点</strong>
          <p>输入和输出节点固定存在。</p>
          {nodeTypes
            .filter((type) => !type.source && !type.sink)
            .map((type) => {
              const count = definition.nodes.filter(
                (node) => node.type === type.type,
              ).length;
              const disabled =
                type.max_instances !== null &&
                count >= type.max_instances;
              return (
                <button
                  key={type.type}
                  type="button"
                  disabled={disabled}
                  onClick={() => addNode(type)}
                >
                  <span>{type.display_name}</span>
                  <small>
                    {type.required_api ?? "内置运行时"}
                    {disabled ? " · 已达到上限" : ""}
                  </small>
                </button>
              );
            })}
        </aside>
        <div className="dag-canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeRenderers}
            onNodesChange={changeNodes}
            onEdgesChange={changeEdges}
            onConnect={connect}
            onNodeDoubleClick={(_event, node) => {
              const target = definition.nodes.find(
                (item) => item.id === node.id,
              );
              if (target) {
                onConfigureNode(target);
              }
            }}
            fitView
            minZoom={0.45}
            maxZoom={1.5}
            deleteKeyCode={["Backspace", "Delete"]}
            proOptions={{ hideAttribution: true }}
          >
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
      </div>

      {diagnostics.length > 0 && (
        <div className="dag-diagnostics" role="alert">
          <strong>处理链还有 {diagnostics.length} 项需要处理</strong>
          <ul>
            {diagnostics.map((item, index) => (
              <li key={`${item.code}-${item.node_id ?? index}`}>
                {item.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      <footer className="dag-editor-actions">
        <span>
          {!saved
            ? "有尚未保存的本地修改"
            : valid
              ? "工作副本已通过校验"
              : "工作副本已保存，尚未通过校验"}
        </span>
        <div>
          <button
            className="button button-quiet"
            type="button"
            disabled={saved || saving}
            onClick={onSave}
          >
            {saving ? "保存中…" : "保存工作副本"}
          </button>
          <button
            className="button button-secondary"
            type="button"
            disabled={!saved || validating}
            onClick={onValidate}
          >
            {validating ? "校验中…" : "校验处理链"}
          </button>
          <button
            className="button button-primary"
            type="button"
            disabled={
              !saved ||
              !valid ||
              !activationNeeded ||
              activating
            }
            onClick={onActivate}
          >
            {activating ? "激活中…" : "激活修订"}
          </button>
        </div>
      </footer>
    </section>
  );
}
