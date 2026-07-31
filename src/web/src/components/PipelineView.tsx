import type {
  EngineAsset,
  HealthStatus,
  NodeTypeDefinition,
  PipelineDefinition,
  PipelineNode,
} from "../api/types";
import {
  capabilityLabel,
  engineState,
  nodeCapability,
  nodeDisplayName,
  protocolLabel,
  topologicalNodes,
} from "../model";
import { ActionMenu } from "./ActionMenu";

interface PipelineViewProps {
  definition: PipelineDefinition;
  engines: Map<string, EngineAsset>;
  nodeTypes: NodeTypeDefinition[];
  health: HealthStatus | null;
  draftChanged: boolean;
  onPickEngine: (node: PipelineNode, type: NodeTypeDefinition) => void;
  onRemoveNode: (node: PipelineNode) => void;
}

function stageState(
  node: PipelineNode,
  engine: EngineAsset | null,
  health: HealthStatus | null,
  draftChanged: boolean,
) {
  if (draftChanged) {
    return { label: "待激活", tone: "pending" as const };
  }
  const runtime = health?.nodes.find((item) => item.node_id === node.id);
  if (runtime?.state === "not_ready") {
    return { label: "不可用", tone: "danger" as const };
  }
  if (engine === null) {
    return { label: "尚未配置", tone: "muted" as const };
  }
  return engineState(engine);
}

function PipelineStage({
  index,
  node,
  type,
  engine,
  health,
  draftChanged,
  onPickEngine,
  onRemoveNode,
  knownType,
}: {
  index: number;
  node: PipelineNode;
  type: NodeTypeDefinition;
  engine: EngineAsset | null;
  health: HealthStatus | null;
  draftChanged: boolean;
  onPickEngine: () => void;
  onRemoveNode: () => void;
  knownType: boolean;
}) {
  const state = stageState(node, engine, health, draftChanged);
  const name = nodeDisplayName(node, [type]);
  const runtime = health?.nodes.find((item) => item.node_id === node.id);

  return (
    <section className="pipeline-stage">
      <div className="stage-index" aria-hidden="true">
        {String(index).padStart(2, "0")}
      </div>
      <div className="stage-body">
        <header className="stage-heading">
          <div>
            <h3>{name}</h3>
            <span>
              {capabilityLabel(nodeCapability(node.type))}
              {!knownType
                ? " · 未知类型"
                : type.required_api
                  ? " · 引擎节点"
                  : " · 内置节点"}
            </span>
          </div>
          <span className={`stage-state stage-state-${state.tone}`}>
            <span aria-hidden="true" />
            {state.label}
          </span>
        </header>

        {type.required_api || engine ? (
          engine ? (
            <div className="stage-binding">
              <strong>{engine.name}</strong>
              <div>
                <code>{engine.target}</code>
                <span aria-hidden="true"> · </span>
                <span>{protocolLabel(engine.protocol)}</span>
              </div>
              {runtime?.reason && <p>{runtime.reason}</p>}
            </div>
          ) : (
            <div className="stage-empty">
              <strong>尚未配置{name}引擎</strong>
              <p>
                选择一个已启用且接口为 {type.required_api} 的引擎后，
                才能通过处理链校验。
              </p>
            </div>
          )
        ) : (
          <div className="stage-binding">
            <strong>
              {knownType
                ? type.source
                  ? "接收会话音频"
                  : "汇总会话事件"
                : "节点类型信息不可用"}
            </strong>
            <div>
              {knownType
                ? "由 Audio Harness 运行时提供，不需要外部引擎。"
                : `服务端未返回 ${node.type} 的节点类型定义；可在拓扑编辑器中查看原始节点。`}
            </div>
          </div>
        )}

        <div className="stage-actions">
          {knownType && type.required_api && (
            <button
              className="text-button"
              type="button"
              onClick={onPickEngine}
            >
              {engine ? "更换引擎" : "选择引擎"}
            </button>
          )}
          {knownType && !type.source && !type.sink && (
            <ActionMenu
              items={[
                {
                  label: "移除节点",
                  tone: "danger",
                  onSelect: onRemoveNode,
                },
              ]}
            />
          )}
        </div>
      </div>
    </section>
  );
}

export function PipelineView({
  definition,
  engines,
  nodeTypes,
  health,
  draftChanged,
  onPickEngine,
  onRemoveNode,
}: PipelineViewProps) {
  const typeByName = new Map(nodeTypes.map((type) => [type.type, type]));
  const ordered = topologicalNodes(definition);
  const source = ordered.find((node) => typeByName.get(node.type)?.source);
  const sink = ordered.find((node) => typeByName.get(node.type)?.sink);
  const stages = ordered.filter((node) => {
    const type = typeByName.get(node.type);
    return !type || (!type.source && !type.sink);
  });
  const branchCount = definition.nodes.filter((node) => {
    const outgoing = new Set(
      definition.edges
        .filter((edge) => edge.source_node === node.id)
        .map((edge) => edge.target_node),
    );
    return outgoing.size > 1;
  }).length;

  if (!source || !sink) {
    return (
      <div className="quiet-state">
        当前工作副本缺少输入或输出节点，请进入拓扑编辑器修复。
      </div>
    );
  }

  return (
    <>
      {branchCount > 0 && (
        <p className="pipeline-structure-note">
          当前处理链包含 {branchCount} 个分支点；此处按拓扑顺序展示，
          完整连接关系请在“编辑拓扑”中查看。
        </p>
      )}
      <div className="pipeline-flow" aria-label="默认语音处理链">
        <div className="pipeline-terminal pipeline-input">
          <span>输入</span>
          <strong>{nodeDisplayName(source, nodeTypes)}</strong>
        </div>
        <div className="pipeline-connector" aria-hidden="true">
          <span>↓</span>
        </div>

        {stages.map((node, index) => {
          const knownType = typeByName.get(node.type);
          const type: NodeTypeDefinition = knownType ?? {
            type: node.type,
            display_name: node.display_name ?? node.type,
            input_ports: {},
            output_ports: {},
            config_schema: {},
            allowed_failure_policies: ["fail_pipeline"],
            default_failure_policy: "fail_pipeline",
            required_api: null,
            max_instances: null,
            source: false,
            sink: false,
            version: 0,
          };
          return (
            <div className="pipeline-stage-group" key={node.id}>
              <PipelineStage
                index={index + 1}
                node={node}
                type={type}
                engine={
                  node.engine_id
                    ? engines.get(node.engine_id) ?? null
                    : null
                }
                health={health}
                draftChanged={draftChanged}
                knownType={knownType !== undefined}
                onPickEngine={() => onPickEngine(node, type)}
                onRemoveNode={() => onRemoveNode(node)}
              />
              <div className="pipeline-connector" aria-hidden="true">
                <span>↓</span>
              </div>
            </div>
          );
        })}

        <div className="pipeline-terminal pipeline-output">
          <span>输出</span>
          <strong>{nodeDisplayName(sink, nodeTypes)}</strong>
        </div>
      </div>
    </>
  );
}
