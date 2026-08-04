from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Mapping

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from audio_harness.assets import (
    EngineApi,
    EngineAssetData,
    EngineProtocol,
    EngineSnapshot,
)


class StrictGraphModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FailurePolicy(str, Enum):
    FAIL_PIPELINE = "fail_pipeline"
    FAIL_OPEN = "fail_open"


class NodePosition(StrictGraphModel):
    x: float = 0
    y: float = 0


class PipelineNode(StrictGraphModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    type: str = Field(min_length=1, max_length=100)
    display_name: str | None = Field(default=None, max_length=200)
    engine_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    failure_policy: FailurePolicy | None = None
    position: NodePosition = Field(default_factory=NodePosition)


class PipelineEdge(StrictGraphModel):
    id: str = Field(min_length=1, max_length=150, pattern=r"^[a-zA-Z0-9_-]+$")
    source_node: str = Field(min_length=1)
    source_port: str = Field(min_length=1)
    target_node: str = Field(min_length=1)
    target_port: str = Field(min_length=1)


class PipelineDefinition(StrictGraphModel):
    schema_version: Literal[1] = 1
    nodes: list[PipelineNode]
    edges: list[PipelineEdge]

    @model_validator(mode="after")
    def unique_identifiers(self) -> "PipelineDefinition":
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("节点 ID 不能重复")
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("连线 ID 不能重复")
        return self


class EmptyNodeConfig(StrictGraphModel):
    pass


class PhysicalVadNodeConfig(StrictGraphModel):
    min_segment_seconds: float = Field(default=0.5, ge=0)
    max_segment_seconds: float = Field(default=60.0, gt=0)
    audio_buffer_seconds: float = Field(default=120.0, gt=0)
    speech_pre_roll_seconds: float = Field(default=0.3, ge=0)

    @model_validator(mode="after")
    def validate_buffer(self) -> "PhysicalVadNodeConfig":
        required = self.max_segment_seconds + self.speech_pre_roll_seconds
        if self.audio_buffer_seconds < required:
            raise ValueError(
                "audio_buffer_seconds 不能小于 "
                "max_segment_seconds + speech_pre_roll_seconds"
            )
        return self


class SemanticVadNodeConfig(StrictGraphModel):
    max_buffer_seconds: float = Field(default=60.0, gt=0)


class ResponsesAgentNodeConfig(StrictGraphModel):
    model: str = Field(
        default="",
        max_length=200,
    )
    thinking_enabled: bool = False
    instructions: str = Field(default="", max_length=32_768)
    max_output_tokens: int = Field(default=1024, ge=1, le=32_768)
    max_tool_rounds: int = Field(default=8, ge=1, le=32)
    max_parallel_tools: int = Field(default=4, ge=1, le=16)
    tool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    service_tools: list[str] = Field(default_factory=list)
    mcp_servers: list["McpServerNodeConfig"] = Field(default_factory=list)
    history_max_items: int = Field(default=128, ge=8, le=4096)


class McpServerNodeConfig(StrictGraphModel):
    server_label: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    server_url: str = Field(min_length=1, max_length=2048)
    require_approval: Literal["always", "never"] | dict[str, Any] = "always"
    allowed_tools: list[str] | None = None
    headers_env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_url(self) -> "McpServerNodeConfig":
        if not self.server_url.startswith(("http://", "https://")):
            raise ValueError("MCP server_url 必须使用 HTTP(S)")
        return self


class DuplexTtsNodeConfig(StrictGraphModel):
    default_voice: str = Field(default="", max_length=200)
    allowed_voices: list[str] = Field(default_factory=list)
    language: str = Field(default="Auto", min_length=1, max_length=64)
    decoder_chunk_size: int = Field(default=0, ge=0, le=1024)

    @model_validator(mode="after")
    def validate_default_voice(self) -> "DuplexTtsNodeConfig":
        if not self.allowed_voices and not self.default_voice:
            return self
        if not self.allowed_voices:
            raise ValueError("allowed_voices 不能为空")
        if self.default_voice not in self.allowed_voices:
            raise ValueError("default_voice 必须位于 allowed_voices 中")
        if len(set(self.allowed_voices)) != len(self.allowed_voices):
            raise ValueError("allowed_voices 不能重复")
        return self


@dataclass(frozen=True)
class PortDefinition:
    payload_type: str
    required: bool = True
    aggregate: bool = False


@dataclass(frozen=True)
class NodeTypeDefinition:
    type: str
    display_name: str
    input_ports: Mapping[str, PortDefinition]
    output_ports: Mapping[str, PortDefinition]
    config_model: type[BaseModel]
    allowed_failure_policies: tuple[FailurePolicy, ...]
    default_failure_policy: FailurePolicy
    required_api: EngineApi | None = None
    max_instances: int | None = None
    source: bool = False
    sink: bool = False
    version: int = 1


class NodeTypeRegistry:
    def __init__(self) -> None:
        self._types: dict[str, NodeTypeDefinition] = {}

    def register(self, definition: NodeTypeDefinition) -> None:
        if definition.type in self._types:
            raise ValueError(f"节点类型已经注册: {definition.type}")
        self._types[definition.type] = definition

    def get(self, node_type: str) -> NodeTypeDefinition | None:
        return self._types.get(node_type)

    def all(self) -> tuple[NodeTypeDefinition, ...]:
        return tuple(self._types[key] for key in sorted(self._types))


def default_node_registry() -> NodeTypeRegistry:
    registry = NodeTypeRegistry()
    registry.register(
        NodeTypeDefinition(
            type="audio_input",
            display_name="音频输入",
            input_ports={},
            output_ports={
                "audio": PortDefinition("audio_chunk"),
            },
            config_model=EmptyNodeConfig,
            allowed_failure_policies=(FailurePolicy.FAIL_PIPELINE,),
            default_failure_policy=FailurePolicy.FAIL_PIPELINE,
            max_instances=1,
            source=True,
        )
    )
    registry.register(
        NodeTypeDefinition(
            type="physical_vad",
            display_name="物理 VAD",
            input_ports={
                "audio": PortDefinition("audio_chunk"),
            },
            output_ports={
                "segments": PortDefinition("audio_segment"),
                "events": PortDefinition("session_event", required=False),
            },
            config_model=PhysicalVadNodeConfig,
            allowed_failure_policies=(FailurePolicy.FAIL_PIPELINE,),
            default_failure_policy=FailurePolicy.FAIL_PIPELINE,
            required_api=EngineApi.UX_VAD,
            max_instances=1,
        )
    )
    registry.register(
        NodeTypeDefinition(
            type="semantic_vad",
            display_name="语义 VAD",
            input_ports={
                "segments": PortDefinition("audio_segment"),
            },
            output_ports={
                "segments": PortDefinition("audio_segment"),
                "events": PortDefinition("session_event", required=False),
            },
            config_model=SemanticVadNodeConfig,
            allowed_failure_policies=(
                FailurePolicy.FAIL_OPEN,
                FailurePolicy.FAIL_PIPELINE,
            ),
            default_failure_policy=FailurePolicy.FAIL_OPEN,
            required_api=EngineApi.UX_VAD,
            max_instances=1,
        )
    )
    registry.register(
        NodeTypeDefinition(
            type="asr",
            display_name="语音识别",
            input_ports={
                "segments": PortDefinition("audio_segment"),
            },
            output_ports={
                "events": PortDefinition("session_event"),
            },
            config_model=EmptyNodeConfig,
            allowed_failure_policies=(FailurePolicy.FAIL_PIPELINE,),
            default_failure_policy=FailurePolicy.FAIL_PIPELINE,
            required_api=EngineApi.UX_SPEECH,
            max_instances=1,
        )
    )
    registry.register(
        NodeTypeDefinition(
            type="responses_agent",
            display_name="Responses Agent",
            input_ports={
                "events": PortDefinition("session_event"),
            },
            output_ports={
                "events": PortDefinition("session_event"),
            },
            config_model=ResponsesAgentNodeConfig,
            allowed_failure_policies=(FailurePolicy.FAIL_PIPELINE,),
            default_failure_policy=FailurePolicy.FAIL_PIPELINE,
            required_api=EngineApi.RESPONSES,
            max_instances=1,
        )
    )
    registry.register(
        NodeTypeDefinition(
            type="duplex_tts",
            display_name="双向流式 TTS",
            input_ports={
                "events": PortDefinition("session_event"),
            },
            output_ports={
                "events": PortDefinition("session_event"),
            },
            config_model=DuplexTtsNodeConfig,
            allowed_failure_policies=(FailurePolicy.FAIL_PIPELINE,),
            default_failure_policy=FailurePolicy.FAIL_PIPELINE,
            required_api=EngineApi.QWEN_TTS_DUPLEX,
            max_instances=1,
        )
    )
    registry.register(
        NodeTypeDefinition(
            type="session_output",
            display_name="会话输出",
            input_ports={
                "events": PortDefinition("session_event", aggregate=True),
            },
            output_ports={},
            config_model=EmptyNodeConfig,
            allowed_failure_policies=(FailurePolicy.FAIL_PIPELINE,),
            default_failure_policy=FailurePolicy.FAIL_PIPELINE,
            max_instances=1,
            sink=True,
        )
    )
    return registry


@dataclass(frozen=True)
class GraphDiagnostic:
    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    port: str | None = None


class GraphValidationError(ValueError):
    def __init__(self, diagnostics: list[GraphDiagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__("; ".join(item.message for item in diagnostics))


@dataclass(frozen=True)
class CompiledNode:
    definition: PipelineNode
    node_type: NodeTypeDefinition
    config: BaseModel
    failure_policy: FailurePolicy
    engine_snapshot: EngineSnapshot | None


@dataclass(frozen=True)
class CompiledPipelinePlan:
    schema_version: int
    definition: PipelineDefinition
    nodes: Mapping[str, CompiledNode]
    edges: tuple[PipelineEdge, ...]
    topological_order: tuple[str, ...]

    @property
    def engine_snapshots(self) -> tuple[EngineSnapshot, ...]:
        by_key = {
            (node.engine_snapshot.id, node.engine_snapshot.revision): (
                node.engine_snapshot
            )
            for node in self.nodes.values()
            if node.engine_snapshot is not None
        }
        return tuple(by_key[key] for key in sorted(by_key))


def compiled_plan_document(plan: CompiledPipelinePlan) -> dict[str, Any]:
    return {
        "schema_version": plan.schema_version,
        "topological_order": list(plan.topological_order),
        "node_type_versions": {
            node_id: node.node_type.version
            for node_id, node in plan.nodes.items()
        },
        "engine_snapshots": {
            node_id: {
                "id": snapshot.id,
                "revision": snapshot.revision,
                "name": snapshot.name,
                "api": snapshot.api.value,
                "protocol": snapshot.protocol.value,
                "target": snapshot.target,
                "connect_timeout_seconds": (
                    snapshot.connect_timeout_seconds
                ),
                "request_timeout_seconds": (
                    snapshot.request_timeout_seconds
                ),
                "credential_env": snapshot.credential_env,
                "api_key": snapshot.api_key,
                "details": snapshot.details,
            }
            for node_id, node in plan.nodes.items()
            if (snapshot := node.engine_snapshot) is not None
        },
    }


def restore_compiled_plan(
    definition: PipelineDefinition,
    document: Mapping[str, Any],
    *,
    registry: NodeTypeRegistry,
) -> CompiledPipelinePlan:
    if document.get("schema_version") != definition.schema_version:
        raise ValueError("执行计划与处理链 schema_version 不一致")
    raw_versions = document.get("node_type_versions")
    raw_snapshots = document.get("engine_snapshots")
    raw_order = document.get("topological_order")
    if (
        not isinstance(raw_versions, dict)
        or not isinstance(raw_snapshots, dict)
        or not isinstance(raw_order, list)
    ):
        raise ValueError("执行计划文档不完整")

    compiled_nodes: dict[str, CompiledNode] = {}
    for node in definition.nodes:
        node_type = registry.get(node.type)
        if node_type is None:
            raise ValueError(f"执行计划引用未知节点类型: {node.type}")
        if raw_versions.get(node.id) != node_type.version:
            raise ValueError(
                f"节点类型版本不兼容: {node.id} "
                f"需要 {raw_versions.get(node.id)}，"
                f"当前为 {node_type.version}"
            )
        policy = node.failure_policy or node_type.default_failure_policy
        config = node_type.config_model.model_validate(node.config)
        snapshot: EngineSnapshot | None = None
        raw_snapshot = raw_snapshots.get(node.id)
        if raw_snapshot is not None:
            if not isinstance(raw_snapshot, dict):
                raise ValueError(f"节点引擎快照不合法: {node.id}")
            snapshot = EngineSnapshot(
                id=str(raw_snapshot["id"]),
                revision=int(raw_snapshot["revision"]),
                name=str(raw_snapshot["name"]),
                api=EngineApi(str(raw_snapshot["api"])),
                protocol=EngineProtocol(str(raw_snapshot["protocol"])),
                target=str(raw_snapshot["target"]),
                connect_timeout_seconds=float(
                    raw_snapshot["connect_timeout_seconds"]
                ),
                request_timeout_seconds=(
                    float(raw_snapshot["request_timeout_seconds"])
                    if raw_snapshot.get("request_timeout_seconds") is not None
                    else None
                ),
                credential_env=(
                    str(raw_snapshot["credential_env"])
                    if raw_snapshot.get("credential_env") is not None
                    else None
                ),
                api_key=(
                    str(raw_snapshot["api_key"])
                    if raw_snapshot.get("api_key") is not None
                    else None
                ),
                details=dict(raw_snapshot.get("details") or {}),
            )
        if node_type.required_api is not None and snapshot is None:
            raise ValueError(f"执行计划缺少节点引擎快照: {node.id}")
        compiled_nodes[node.id] = CompiledNode(
            definition=node,
            node_type=node_type,
            config=config,
            failure_policy=policy,
            engine_snapshot=snapshot,
        )

    order = tuple(str(item) for item in raw_order)
    if set(order) != set(compiled_nodes):
        raise ValueError("执行计划拓扑顺序与节点集合不一致")
    return CompiledPipelinePlan(
        schema_version=definition.schema_version,
        definition=definition,
        nodes=compiled_nodes,
        edges=tuple(definition.edges),
        topological_order=order,
    )


def _engine_snapshot(asset: EngineAssetData) -> EngineSnapshot:
    return asset.snapshot()


def compile_pipeline(
    definition: PipelineDefinition,
    *,
    registry: NodeTypeRegistry,
    assets: Mapping[str, EngineAssetData],
) -> CompiledPipelinePlan:
    diagnostics: list[GraphDiagnostic] = []
    node_types: dict[str, NodeTypeDefinition] = {}
    parsed_configs: dict[str, BaseModel] = {}
    failure_policies: dict[str, FailurePolicy] = {}
    engine_snapshots: dict[str, EngineSnapshot | None] = {}
    counts: dict[str, int] = {}

    for node in definition.nodes:
        node_type = registry.get(node.type)
        if node_type is None:
            diagnostics.append(
                GraphDiagnostic(
                    "unknown_node_type",
                    f"未知节点类型: {node.type}",
                    node_id=node.id,
                )
            )
            continue
        node_types[node.id] = node_type
        counts[node.type] = counts.get(node.type, 0) + 1
        try:
            parsed_configs[node.id] = node_type.config_model.model_validate(
                node.config
            )
        except Exception as exc:
            diagnostics.append(
                GraphDiagnostic(
                    "invalid_node_config",
                    f"{node_type.display_name}配置不合法: {exc}",
                    node_id=node.id,
                )
            )
        policy = node.failure_policy or node_type.default_failure_policy
        if policy not in node_type.allowed_failure_policies:
            diagnostics.append(
                GraphDiagnostic(
                    "invalid_failure_policy",
                    f"{node_type.display_name}不支持失败策略 {policy.value}",
                    node_id=node.id,
                )
            )
        failure_policies[node.id] = policy

        if node_type.required_api is None:
            if node.engine_id is not None:
                diagnostics.append(
                    GraphDiagnostic(
                        "unexpected_engine",
                        f"{node_type.display_name}不需要绑定引擎",
                        node_id=node.id,
                    )
                )
            engine_snapshots[node.id] = None
            continue
        if node.engine_id is None:
            diagnostics.append(
                GraphDiagnostic(
                    "missing_engine",
                    f"{node_type.display_name}尚未绑定引擎",
                    node_id=node.id,
                )
            )
            continue
        asset = assets.get(node.engine_id)
        if asset is None:
            diagnostics.append(
                GraphDiagnostic(
                    "engine_not_found",
                    f"{node_type.display_name}引用的引擎不存在",
                    node_id=node.id,
                )
            )
            continue
        if not asset.enabled:
            diagnostics.append(
                GraphDiagnostic(
                    "engine_disabled",
                    f"{node_type.display_name}引用的引擎已停用",
                    node_id=node.id,
                )
            )
        if asset.api is not node_type.required_api:
            diagnostics.append(
                GraphDiagnostic(
                    "engine_api_mismatch",
                    f"{node_type.display_name}需要 {node_type.required_api.value}，"
                    f"实际为 {asset.api.value}",
                    node_id=node.id,
                )
            )
        engine_snapshots[node.id] = _engine_snapshot(asset)

    for node_type in registry.all():
        count = counts.get(node_type.type, 0)
        if node_type.max_instances is not None and count > node_type.max_instances:
            diagnostics.append(
                GraphDiagnostic(
                    "too_many_nodes",
                    f"{node_type.display_name}最多允许 "
                    f"{node_type.max_instances} 个节点",
                )
            )
        if (node_type.source or node_type.sink) and count != 1:
            diagnostics.append(
                GraphDiagnostic(
                    "required_terminal",
                    f"处理链必须且只能包含一个{node_type.display_name}",
                )
            )

    graph = nx.DiGraph()
    graph.add_nodes_from(node.id for node in definition.nodes)
    incoming: dict[tuple[str, str], list[PipelineEdge]] = {}
    seen_connections: set[tuple[str, str, str, str]] = set()
    valid_edges: list[PipelineEdge] = []
    node_ids = {node.id for node in definition.nodes}

    for edge in definition.edges:
        if edge.source_node not in node_ids or edge.target_node not in node_ids:
            diagnostics.append(
                GraphDiagnostic(
                    "edge_node_not_found",
                    "连线引用了不存在的节点",
                    edge_id=edge.id,
                )
            )
            continue
        source_type = node_types.get(edge.source_node)
        target_type = node_types.get(edge.target_node)
        if source_type is None or target_type is None:
            continue
        source_port = source_type.output_ports.get(edge.source_port)
        target_port = target_type.input_ports.get(edge.target_port)
        if source_port is None:
            diagnostics.append(
                GraphDiagnostic(
                    "source_port_not_found",
                    f"源端口不存在: {edge.source_port}",
                    edge_id=edge.id,
                    node_id=edge.source_node,
                    port=edge.source_port,
                )
            )
            continue
        if target_port is None:
            diagnostics.append(
                GraphDiagnostic(
                    "target_port_not_found",
                    f"目标端口不存在: {edge.target_port}",
                    edge_id=edge.id,
                    node_id=edge.target_node,
                    port=edge.target_port,
                )
            )
            continue
        if source_port.payload_type != target_port.payload_type:
            diagnostics.append(
                GraphDiagnostic(
                    "port_type_mismatch",
                    f"端口类型不兼容: {source_port.payload_type} → "
                    f"{target_port.payload_type}",
                    edge_id=edge.id,
                )
            )
            continue
        connection = (
            edge.source_node,
            edge.source_port,
            edge.target_node,
            edge.target_port,
        )
        if connection in seen_connections:
            diagnostics.append(
                GraphDiagnostic(
                    "duplicate_edge",
                    "不能重复连接同一对端口",
                    edge_id=edge.id,
                )
            )
            continue
        seen_connections.add(connection)
        incoming.setdefault(
            (edge.target_node, edge.target_port),
            [],
        ).append(edge)
        graph.add_edge(edge.source_node, edge.target_node)
        valid_edges.append(edge)

    for node_id, node_type in node_types.items():
        for port_name, port in node_type.input_ports.items():
            edges = incoming.get((node_id, port_name), [])
            if port.required and not edges:
                diagnostics.append(
                    GraphDiagnostic(
                        "required_input_missing",
                        f"{node_type.display_name}缺少必需输入 {port_name}",
                        node_id=node_id,
                        port=port_name,
                    )
                )
            if len(edges) > 1 and not port.aggregate:
                diagnostics.append(
                    GraphDiagnostic(
                        "implicit_fan_in",
                        f"{node_type.display_name}.{port_name} 不允许多路汇合",
                        node_id=node_id,
                        port=port_name,
                    )
                )

    if not nx.is_directed_acyclic_graph(graph):
        diagnostics.append(
            GraphDiagnostic("cycle_detected", "处理链不能包含循环")
        )
        topological_order: tuple[str, ...] = ()
    else:
        topological_order = tuple(
            nx.lexicographical_topological_sort(graph, key=str)
        )
        source_ids = [
            node_id
            for node_id, node_type in node_types.items()
            if node_type.source
        ]
        sink_ids = [
            node_id
            for node_id, node_type in node_types.items()
            if node_type.sink
        ]
        if len(source_ids) == 1 and len(sink_ids) == 1:
            source_id = source_ids[0]
            sink_id = sink_ids[0]
            reachable = {source_id, *nx.descendants(graph, source_id)}
            reaches_sink = {sink_id, *nx.ancestors(graph, sink_id)}
            for node_id in sorted(node_ids):
                if node_id not in reachable:
                    diagnostics.append(
                        GraphDiagnostic(
                            "node_not_reachable",
                            "节点无法从音频输入到达",
                            node_id=node_id,
                        )
                    )
                if node_id not in reaches_sink:
                    diagnostics.append(
                        GraphDiagnostic(
                            "node_has_no_output_path",
                            "节点没有通往会话输出的路径",
                            node_id=node_id,
                        )
                    )

    if diagnostics:
        raise GraphValidationError(diagnostics)

    compiled_nodes = {
        node.id: CompiledNode(
            definition=node,
            node_type=node_types[node.id],
            config=parsed_configs[node.id],
            failure_policy=failure_policies[node.id],
            engine_snapshot=engine_snapshots.get(node.id),
        )
        for node in definition.nodes
    }
    return CompiledPipelinePlan(
        schema_version=definition.schema_version,
        definition=definition,
        nodes=compiled_nodes,
        edges=tuple(valid_edges),
        topological_order=topological_order,
    )
