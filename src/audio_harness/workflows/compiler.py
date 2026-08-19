from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel

from audio_harness.engines.models import EngineApi, EngineAssetData, EngineProtocol, EngineSnapshot
from audio_harness.workflows.models import (
    AsrNode,
    AsrNodeConfig,
    AudioEmbeddingNodeConfig,
    VectorSearchNodeConfig,
    AudioEdge,
    AudioNode,
    AudioWorkflowDocument,
    FailurePolicy,
    GraphInputRef,
    NodeOutputRef,
    PhysicalVadConfig,
    PhysicalVadNode,
    SpeakerDiarizationNodeConfig,
    SpeakerClusteringNodeConfig,
    SpeakerFilterNodeConfig,
    SemanticVadNode,
    SemanticVadConfig,
)


class PortKind(str, Enum):
    PCM_CHUNK = "pcm_chunk"
    AUDIO_SEGMENT = "audio_segment"
    SPEECH_STARTED = "speech_started"
    SPEECH_STOPPED = "speech_stopped"
    TRANSCRIPTION = "transcription"
    EMBEDDING = "embedding"
    VECTOR_SEARCH_RESULT = "vector_search_result"
    SPEAKER_FILTER_RESULT = "speaker_filter_result"
    DIARIZATION_LOCAL_RESULT = "diarization_local_result"
    SPEAKER_TRANSCRIPTION = "speaker_transcription"
    DIARIZATION_SEGMENT = "diarization_segment"
    DIARIZATION_CLUSTER_RESULT = "diarization_cluster_result"


@dataclass(frozen=True)
class PortSpec:
    kind: PortKind
    accepted_kinds: tuple[PortKind, ...] = ()

    def accepts(self, source_kind: PortKind) -> bool:
        return source_kind is self.kind or source_kind in self.accepted_kinds


@dataclass(frozen=True)
class AudioNodeSpec:
    kind: str
    display_name: str
    inputs: Mapping[str, PortSpec]
    outputs: Mapping[str, PortSpec]
    required_api: EngineApi | None
    config_model: type[BaseModel]
    required_capability: str | None = None
    version: int = 1


_NODE_SPECS: dict[str, AudioNodeSpec] = {
    "physical_vad": AudioNodeSpec(
        kind="physical_vad",
        display_name="物理 VAD",
        inputs={"audio": PortSpec(PortKind.PCM_CHUNK)},
        outputs={
            "segments": PortSpec(PortKind.AUDIO_SEGMENT),
            "speech_started": PortSpec(PortKind.SPEECH_STARTED),
            "speech_stopped": PortSpec(PortKind.SPEECH_STOPPED),
        },
        required_api=EngineApi.UX_VAD,
        config_model=PhysicalVadConfig,
    ),
    "semantic_vad": AudioNodeSpec(
        kind="semantic_vad",
        display_name="语义 VAD",
        inputs={"segments": PortSpec(PortKind.AUDIO_SEGMENT)},
        outputs={
            "segments": PortSpec(PortKind.AUDIO_SEGMENT),
            "speech_stopped": PortSpec(PortKind.SPEECH_STOPPED),
            "turn_progress": PortSpec(PortKind.TRANSCRIPTION),
        },
        required_api=EngineApi.UX_VAD,
        config_model=SemanticVadConfig,
    ),
    "asr": AudioNodeSpec(
        kind="asr",
        display_name="语音识别",
        inputs={
            "segments": PortSpec(
                PortKind.AUDIO_SEGMENT,
                accepted_kinds=(PortKind.DIARIZATION_SEGMENT,),
            )
        },
        outputs={
            "transcripts": PortSpec(PortKind.TRANSCRIPTION),
            "speaker_transcripts": PortSpec(PortKind.SPEAKER_TRANSCRIPTION),
        },
        required_api=EngineApi.UX_SPEECH,
        config_model=AsrNodeConfig,
    ),
    "audio_embedding": AudioNodeSpec(
        kind="audio_embedding", display_name="音频向量化",
        inputs={"segments": PortSpec(PortKind.AUDIO_SEGMENT)},
        outputs={"embeddings": PortSpec(PortKind.EMBEDDING)},
        required_api=EngineApi.ENGINE_FEATURE, config_model=AudioEmbeddingNodeConfig,
    ),
    "speaker_diarization": AudioNodeSpec(
        kind="speaker_diarization",
        display_name="说话人分离",
        inputs={"segments": PortSpec(PortKind.AUDIO_SEGMENT)},
        outputs={
            "segments": PortSpec(PortKind.DIARIZATION_SEGMENT),
            "local_results": PortSpec(PortKind.DIARIZATION_LOCAL_RESULT),
        },
        required_api=EngineApi.UX_SPEAKER_DIARIZATION,
        config_model=SpeakerDiarizationNodeConfig,
    ),
    "speaker_clustering": AudioNodeSpec(
        kind="speaker_clustering",
        display_name="说话人聚类",
        inputs={"segments": PortSpec(PortKind.DIARIZATION_SEGMENT)},
        outputs={
            "results": PortSpec(PortKind.DIARIZATION_CLUSTER_RESULT),
        },
        required_api=EngineApi.ENGINE_FEATURE,
        config_model=SpeakerClusteringNodeConfig,
    ),
    "vector_search": AudioNodeSpec(
        kind="vector_search", display_name="人物搜索",
        inputs={"embeddings": PortSpec(PortKind.EMBEDDING)},
        outputs={"results": PortSpec(PortKind.VECTOR_SEARCH_RESULT)},
        required_api=None, config_model=VectorSearchNodeConfig,
    ),
    "speaker_filter": AudioNodeSpec(
        kind="speaker_filter",
        display_name="说话人过滤",
        inputs={
            "segments": PortSpec(PortKind.AUDIO_SEGMENT),
            "results": PortSpec(PortKind.VECTOR_SEARCH_RESULT),
        },
        outputs={
            "segments": PortSpec(PortKind.AUDIO_SEGMENT),
            "speech_started": PortSpec(PortKind.SPEECH_STARTED),
            "speech_stopped": PortSpec(PortKind.SPEECH_STOPPED),
            "decisions": PortSpec(PortKind.SPEAKER_FILTER_RESULT),
        },
        required_api=None,
        config_model=SpeakerFilterNodeConfig,
    ),
}


_REQUIRED_OUTPUTS = {
    "primary_transcript": PortKind.TRANSCRIPTION,
    "speech_started": PortKind.SPEECH_STARTED,
    "speech_stopped": PortKind.SPEECH_STOPPED,
}

_OPTIONAL_OUTPUTS = {
    "interim_transcript": PortKind.TRANSCRIPTION,
    "turn_progress": PortKind.TRANSCRIPTION,
    "vector_search": PortKind.VECTOR_SEARCH_RESULT,
    "speaker_filter": PortKind.SPEAKER_FILTER_RESULT,
    "diarization_local": PortKind.DIARIZATION_LOCAL_RESULT,
    "speaker_transcript": PortKind.SPEAKER_TRANSCRIPTION,
    "diarization_cluster": PortKind.DIARIZATION_CLUSTER_RESULT,
}

_KNOWN_OUTPUTS = {**_REQUIRED_OUTPUTS, **_OPTIONAL_OUTPUTS}


@dataclass(frozen=True)
class AudioGraphDiagnostic:
    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    port: str | None = None


class AudioGraphValidationError(ValueError):
    def __init__(self, diagnostics: list[AudioGraphDiagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__("; ".join(item.message for item in diagnostics))


@dataclass(frozen=True)
class CompiledAudioNode:
    definition: AudioNode
    spec: AudioNodeSpec
    engine_snapshot: EngineSnapshot | None


@dataclass(frozen=True)
class CompiledAudioGraph:
    definition: AudioWorkflowDocument
    nodes: Mapping[str, CompiledAudioNode]
    edges: tuple[AudioEdge, ...]
    topological_order: tuple[str, ...]

    @property
    def engine_snapshots(self) -> tuple[EngineSnapshot, ...]:
        snapshots = {
            (node.engine_snapshot.id, node.engine_snapshot.revision): node.engine_snapshot
            for node in self.nodes.values()
            if node.engine_snapshot is not None
        }
        return tuple(snapshots[key] for key in sorted(snapshots))


def speech_started_physical_vad(
    plan: CompiledAudioGraph,
) -> tuple[str, PhysicalVadNode] | None:
    """Find the physical VAD that owns the public speech-started lineage."""

    binding = next(
        (
            output
            for output in plan.definition.outputs
            if output.name == "speech_started"
        ),
        None,
    )
    if binding is None:
        return None
    segment_sources = {
        edge.target.node_id: edge.source.node_id
        for edge in plan.edges
        if edge.target.port == "segments"
        and isinstance(edge.source, NodeOutputRef)
    }
    node_id = binding.source.node_id
    visited: set[str] = set()
    while node_id not in visited:
        visited.add(node_id)
        node = plan.nodes.get(node_id)
        if node is None:
            return None
        if isinstance(node.definition, PhysicalVadNode):
            return node_id, node.definition
        upstream_id = segment_sources.get(node_id)
        if upstream_id is None:
            return None
        node_id = upstream_id
    return None


def audio_node_catalog() -> tuple[AudioNodeSpec, ...]:
    return tuple(_NODE_SPECS[key] for key in sorted(_NODE_SPECS))


def _snapshot(asset: EngineAssetData) -> EngineSnapshot:
    return asset.snapshot()


def _topological_order(
    node_ids: set[str],
    adjacency: Mapping[str, set[str]],
) -> tuple[str, ...] | None:
    indegree = {node_id: 0 for node_id in node_ids}
    for targets in adjacency.values():
        for target in targets:
            indegree[target] += 1
    ready = sorted(node_id for node_id, value in indegree.items() if value == 0)
    result: list[str] = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for target in sorted(adjacency.get(current, ())):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    return tuple(result) if len(result) == len(node_ids) else None


def _reachable_from_input(
    input_targets: set[str],
    adjacency: Mapping[str, set[str]],
) -> set[str]:
    seen: set[str] = set()
    pending = list(input_targets)
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(adjacency.get(current, ()))
    return seen


def _ancestors(
    output_nodes: set[str],
    reverse: Mapping[str, set[str]],
) -> set[str]:
    seen: set[str] = set()
    pending = list(output_nodes)
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(reverse.get(current, ()))
    return seen


def compile_audio_graph(
    definition: AudioWorkflowDocument,
    *,
    assets: Mapping[str, EngineAssetData],
) -> CompiledAudioGraph:
    diagnostics: list[AudioGraphDiagnostic] = []
    node_ids = {node.id for node in definition.nodes}
    nodes_by_id = {node.id: node for node in definition.nodes}
    compiled: dict[str, CompiledAudioNode] = {}

    for node in definition.nodes:
        spec = _NODE_SPECS[node.kind]
        if spec.required_api is None:
            compiled[node.id] = CompiledAudioNode(node, spec, None)
            continue
        if node.engine_id is None:
            diagnostics.append(
                AudioGraphDiagnostic(
                    "missing_engine",
                    f"{spec.display_name}尚未绑定引擎",
                    node_id=node.id,
                )
            )
            continue
        asset = assets.get(node.engine_id)
        if asset is None:
            diagnostics.append(
                AudioGraphDiagnostic(
                    "engine_not_found",
                    f"{spec.display_name}引用的引擎不存在",
                    node_id=node.id,
                )
            )
            continue
        if not asset.enabled:
            diagnostics.append(
                AudioGraphDiagnostic(
                    "engine_disabled",
                    f"{spec.display_name}引用的引擎已停用",
                    node_id=node.id,
                )
            )
        if asset.api is not spec.required_api:
            diagnostics.append(
                AudioGraphDiagnostic(
                    "engine_api_mismatch",
                    f"{spec.display_name}需要 {spec.required_api.value}，实际为 {asset.api.value}",
                    node_id=node.id,
                )
            )
        if spec.required_capability is not None:
            capabilities = asset.details.get("capabilities", ())
            if (
                not isinstance(capabilities, (list, tuple, set))
                or spec.required_capability not in capabilities
            ):
                diagnostics.append(
                    AudioGraphDiagnostic(
                        "engine_capability_mismatch",
                        f"{spec.display_name}需要引擎能力 "
                        f"{spec.required_capability}",
                        node_id=node.id,
                    )
                )
        compiled[node.id] = CompiledAudioNode(
            definition=node,
            spec=spec,
            engine_snapshot=_snapshot(asset),
        )

    incoming: dict[tuple[str, str], AudioEdge] = {}
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    reverse: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    input_targets: set[str] = set()
    connections: set[tuple[str, str, str, str]] = set()
    valid_edges: list[AudioEdge] = []

    for edge in definition.edges:
        target_node = nodes_by_id.get(edge.target.node_id)
        if target_node is None:
            diagnostics.append(
                AudioGraphDiagnostic(
                    "edge_node_not_found",
                    "连线目标引用不存在的节点",
                    edge_id=edge.id,
                    node_id=edge.target.node_id,
                )
            )
            continue
        target_spec = _NODE_SPECS[target_node.kind]
        target_port = target_spec.inputs.get(edge.target.port)
        if target_port is None:
            diagnostics.append(
                AudioGraphDiagnostic(
                    "target_port_not_found",
                    f"目标端口不存在: {edge.target.port}",
                    edge_id=edge.id,
                    node_id=edge.target.node_id,
                    port=edge.target.port,
                )
            )
            continue

        source_node_id = "$input"
        source_port_name = "audio"
        source_kind = PortKind.PCM_CHUNK
        if isinstance(edge.source, NodeOutputRef):
            source_node = nodes_by_id.get(edge.source.node_id)
            if source_node is None:
                diagnostics.append(
                    AudioGraphDiagnostic(
                        "edge_node_not_found",
                        "连线源引用不存在的节点",
                        edge_id=edge.id,
                        node_id=edge.source.node_id,
                    )
                )
                continue
            source_spec = _NODE_SPECS[source_node.kind]
            source_port = source_spec.outputs.get(edge.source.port)
            if source_port is None:
                diagnostics.append(
                    AudioGraphDiagnostic(
                        "source_port_not_found",
                        f"源端口不存在: {edge.source.port}",
                        edge_id=edge.id,
                        node_id=edge.source.node_id,
                        port=edge.source.port,
                    )
                )
                continue
            source_node_id = edge.source.node_id
            source_port_name = edge.source.port
            source_kind = source_port.kind

        if not target_port.accepts(source_kind):
            diagnostics.append(
                AudioGraphDiagnostic(
                    "port_type_mismatch",
                    f"端口类型不兼容: {source_kind.value} → {target_port.kind.value}",
                    edge_id=edge.id,
                )
            )
            continue
        connection = (
            source_node_id,
            source_port_name,
            edge.target.node_id,
            edge.target.port,
        )
        if connection in connections:
            diagnostics.append(
                AudioGraphDiagnostic("duplicate_edge", "不能重复连接同一对端口", edge_id=edge.id)
            )
            continue
        connections.add(connection)
        input_key = (edge.target.node_id, edge.target.port)
        if input_key in incoming:
            diagnostics.append(
                AudioGraphDiagnostic(
                    "implicit_fan_in",
                    f"{target_spec.display_name}.{edge.target.port} 不允许多路汇合",
                    node_id=edge.target.node_id,
                    edge_id=edge.id,
                    port=edge.target.port,
                )
            )
            continue
        incoming[input_key] = edge
        if isinstance(edge.source, GraphInputRef):
            input_targets.add(edge.target.node_id)
        else:
            adjacency[edge.source.node_id].add(edge.target.node_id)
            reverse[edge.target.node_id].add(edge.source.node_id)
        valid_edges.append(edge)

    for node in definition.nodes:
        spec = _NODE_SPECS[node.kind]
        for port_name in spec.inputs:
            if (node.id, port_name) not in incoming:
                diagnostics.append(
                    AudioGraphDiagnostic(
                        "required_input_missing",
                        f"{spec.display_name}缺少必需输入 {port_name}",
                        node_id=node.id,
                        port=port_name,
                    )
                )
        if node.kind != "speaker_filter":
            continue
        segment_edge = incoming.get((node.id, "segments"))
        result_edge = incoming.get((node.id, "results"))
        if segment_edge is None or result_edge is None:
            continue
        result_source = result_edge.source
        if not isinstance(result_source, NodeOutputRef):
            continue
        search_node = nodes_by_id.get(result_source.node_id)
        if search_node is None or search_node.kind != "vector_search":
            continue
        embedding_edge = incoming.get((search_node.id, "embeddings"))
        if embedding_edge is None or not isinstance(
            embedding_edge.source,
            NodeOutputRef,
        ):
            continue
        embedding_node = nodes_by_id.get(embedding_edge.source.node_id)
        if embedding_node is None or embedding_node.kind != "audio_embedding":
            continue
        embedding_segment_edge = incoming.get((embedding_node.id, "segments"))
        if (
            embedding_segment_edge is None
            or embedding_segment_edge.source != segment_edge.source
        ):
            diagnostics.append(
                AudioGraphDiagnostic(
                    "speaker_filter_source_mismatch",
                    "说话人过滤的音频与人物搜索必须来自同一个片段出口",
                    node_id=node.id,
                )
            )

    output_nodes: set[str] = set()
    outputs_by_name = {output.name: output for output in definition.outputs}
    for output in definition.outputs:
        source_node = nodes_by_id.get(output.source.node_id)
        if source_node is None:
            diagnostics.append(
                AudioGraphDiagnostic(
                    "output_node_not_found",
                    f"出口 {output.name} 引用了不存在的节点",
                    node_id=output.source.node_id,
                )
            )
            continue
        source_port = _NODE_SPECS[source_node.kind].outputs.get(output.source.port)
        if source_port is None:
            diagnostics.append(
                AudioGraphDiagnostic(
                    "output_port_not_found",
                    f"出口 {output.name} 引用了不存在的端口",
                    node_id=source_node.id,
                    port=output.source.port,
                )
            )
            continue
        expected = _KNOWN_OUTPUTS.get(output.name)
        if expected is not None and source_port.kind is not expected:
            diagnostics.append(
                AudioGraphDiagnostic(
                    "output_type_mismatch",
                    f"出口 {output.name} 必须连接 {expected.value}",
                    node_id=source_node.id,
                    port=output.source.port,
                )
            )
        output_nodes.add(source_node.id)
    for name in _REQUIRED_OUTPUTS:
        if name not in outputs_by_name:
            diagnostics.append(
                AudioGraphDiagnostic("required_output_missing", f"缺少必需出口 {name}")
            )

    primary = outputs_by_name.get("primary_transcript")
    if primary is not None:
        primary_ancestors = _ancestors({primary.source.node_id}, reverse)
        primary_filters = [
            node
            for node in definition.nodes
            if node.kind == "speaker_filter" and node.id in primary_ancestors
        ]
        if primary_filters:
            active_filter = primary_filters[-1]
            required_filter_outputs = {
                "speech_started": "speech_started",
                "speech_stopped": "speech_stopped",
                "speaker_filter": "decisions",
            }
            for output_name, port_name in required_filter_outputs.items():
                binding = outputs_by_name.get(output_name)
                if binding is None or binding.source != NodeOutputRef(
                    node_id=active_filter.id,
                    port=port_name,
                ):
                    diagnostics.append(
                        AudioGraphDiagnostic(
                            "speaker_filter_output_mismatch",
                            f"出口 {output_name} 必须连接说话人过滤节点",
                            node_id=active_filter.id,
                            port=port_name,
                        )
                    )

    order = _topological_order(node_ids, adjacency)
    if order is None:
        diagnostics.append(AudioGraphDiagnostic("cycle_detected", "语音图不能包含循环"))
        order = ()
    else:
        reachable = _reachable_from_input(input_targets, adjacency)
        reaches_output = _ancestors(output_nodes, reverse)
        for node_id in sorted(node_ids):
            if node_id not in reachable:
                diagnostics.append(
                    AudioGraphDiagnostic(
                        "node_not_reachable",
                        "节点无法从音频输入到达",
                        node_id=node_id,
                    )
                )
            if node_id not in reaches_output:
                diagnostics.append(
                    AudioGraphDiagnostic(
                        "node_has_no_output_path",
                        "节点没有通往声明出口的路径",
                        node_id=node_id,
                    )
                )

    if diagnostics:
        raise AudioGraphValidationError(diagnostics)
    return CompiledAudioGraph(
        definition=definition,
        nodes=compiled,
        edges=tuple(valid_edges),
        topological_order=order,
    )


def _snapshot_document(snapshot: EngineSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "revision": snapshot.revision,
        "name": snapshot.name,
        "api": snapshot.api.value,
        "protocol": snapshot.protocol.value,
        "target": snapshot.target,
        "connect_timeout_seconds": snapshot.connect_timeout_seconds,
        "request_timeout_seconds": snapshot.request_timeout_seconds,
        "credential_env": snapshot.credential_env,
        "api_key": snapshot.api_key,
        "details": snapshot.details,
    }


def compiled_audio_graph_document(plan: CompiledAudioGraph) -> dict[str, Any]:
    return {
        "schema_version": plan.definition.schema_version,
        "topological_order": list(plan.topological_order),
        "node_type_versions": {
            node_id: node.spec.version for node_id, node in plan.nodes.items()
        },
        "engine_snapshots": {
            node_id: _snapshot_document(node.engine_snapshot)
            for node_id, node in plan.nodes.items()
            if node.engine_snapshot is not None
        },
    }


def restore_audio_graph(
    definition: AudioWorkflowDocument,
    document: Mapping[str, Any],
) -> CompiledAudioGraph:
    if document.get("schema_version") != definition.schema_version:
        raise ValueError("执行计划与语音图 schema_version 不一致")
    raw_order = document.get("topological_order")
    raw_versions = document.get("node_type_versions")
    raw_snapshots = document.get("engine_snapshots")
    if not isinstance(raw_order, list) or not isinstance(raw_versions, dict) or not isinstance(raw_snapshots, dict):
        raise ValueError("执行计划文档不完整")
    compiled: dict[str, CompiledAudioNode] = {}
    for node in definition.nodes:
        spec = _NODE_SPECS[node.kind]
        if raw_versions.get(node.id) != spec.version:
            raise ValueError(f"节点类型版本不兼容: {node.id}")
        raw = raw_snapshots.get(node.id)
        if spec.required_api is None:
            compiled[node.id] = CompiledAudioNode(node, spec, None)
            continue
        if not isinstance(raw, dict): raise ValueError(f"执行计划缺少节点引擎快照: {node.id}")
        snapshot = EngineSnapshot(
            id=str(raw["id"]),
            revision=int(raw["revision"]),
            name=str(raw["name"]),
            api=EngineApi(str(raw["api"])),
            protocol=EngineProtocol(str(raw["protocol"])),
            target=str(raw["target"]),
            connect_timeout_seconds=float(raw["connect_timeout_seconds"]),
            request_timeout_seconds=(
                float(raw["request_timeout_seconds"])
                if raw.get("request_timeout_seconds") is not None
                else None
            ),
            credential_env=(str(raw["credential_env"]) if raw.get("credential_env") is not None else None),
            api_key=(str(raw["api_key"]) if raw.get("api_key") is not None else None),
            details=dict(raw.get("details") or {}),
        )
        if snapshot.api is not spec.required_api:
            raise ValueError(f"节点引擎快照 API 不匹配: {node.id}")
        if spec.required_capability is not None:
            capabilities = snapshot.details.get("capabilities", ())
            if (
                not isinstance(capabilities, (list, tuple, set))
                or spec.required_capability not in capabilities
            ):
                raise ValueError(f"节点引擎快照能力不匹配: {node.id}")
        compiled[node.id] = CompiledAudioNode(node, spec, snapshot)
    order = tuple(str(item) for item in raw_order)
    if set(order) != set(compiled):
        raise ValueError("执行计划拓扑顺序与节点集合不一致")
    return CompiledAudioGraph(definition, compiled, tuple(definition.edges), order)
