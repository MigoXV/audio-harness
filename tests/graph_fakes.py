from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from audio_harness.engines.models import (
    EngineApi,
    EngineAssetData,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.workflows.compiler import compile_audio_graph
from audio_harness.workflows.models import (
    AsrNode,
    AudioEmbeddingNode,
    AudioEmbeddingNodeConfig,
    AudioEdge,
    AudioWorkflowDocument,
    GraphInputRef,
    NamedOutputBinding,
    NodeInputRef,
    NodeOutputRef,
    PhysicalVadConfig,
    PhysicalVadNode,
    SemanticVadNode,
    SpeakerFilterNode,
    SpeakerFilterNodeConfig,
    VectorSearchNode,
    VectorSearchNodeConfig,
)
from audio_harness.workflows.service import ActiveAudioWorkflow


def _asset(
    asset_id: str,
    *,
    kind: EngineKind,
    api: EngineApi,
) -> EngineAssetData:
    now = datetime.now(timezone.utc)
    return EngineAssetData(
        id=asset_id,
        slug=asset_id,
        name=asset_id,
        kind=kind,
        api=api,
        protocol=EngineProtocol.GRPC,
        target="test:1",
        connect_timeout_seconds=1,
        request_timeout_seconds=None,
        credential_env=None,
        api_key=None,
        enabled=True,
        status=EngineStatus.CONFIRMED_RUNNING,
        image=None,
        description=None,
        status_basis=None,
        status_observed_at=None,
        last_success_at=None,
        probe_latency_ms=None,
        consecutive_failures=0,
        next_probe_at=None,
        source=None,
        details={},
        revision=1,
        created_at=now,
        updated_at=now,
    )


class StaticInferencerPool:
    def __init__(self, inferencers: dict[tuple[str, int], object]) -> None:
        self._inferencers = inferencers
        self.active_leases = 0

    @asynccontextmanager
    async def lease(self, snapshots):
        keys = {(snapshot.id, snapshot.revision) for snapshot in snapshots}
        self.active_leases += 1
        try:
            yield {key: self._inferencers[key] for key in keys}
        finally:
            self.active_leases -= 1


class StaticAudioWorkflowService:
    def __init__(self, workflow: ActiveAudioWorkflow) -> None:
        self._workflow = workflow

    def current(self, workflow_id: str) -> ActiveAudioWorkflow:
        if workflow_id != self._workflow.id:
            raise RuntimeError(f"语音工作流不存在: {workflow_id}")
        return self._workflow


class StaticRouteService:
    def __init__(self, target) -> None:
        self.target = target

    async def resolve(self, model: str, *, allow_internal: bool = False):
        del allow_internal
        if model != "gpt-4o-transcribe":
            raise RuntimeError(f"模型路由不存在: {model}")
        return self.target


def static_audio_workflow(
    *,
    asr: object,
    vad: object,
    semantic_vad: object | None = None,
    physical_config: PhysicalVadConfig | None = None,
    speaker_filter: bool = False,
) -> tuple[StaticInferencerPool, StaticAudioWorkflowService, ActiveAudioWorkflow]:
    config = physical_config or PhysicalVadConfig(
        min_segment_seconds=0,
        max_segment_seconds=60,
        audio_buffer_seconds=61,
        speech_pre_roll_seconds=0,
    )
    nodes = [
        PhysicalVadNode(
            id="physical-vad",
            engine_id="vad-engine",
            config=config,
        ),
        AsrNode(id="asr", engine_id="asr-engine"),
    ]
    edges = [
        AudioEdge(
            id="audio-vad",
            source=GraphInputRef(),
            target=NodeInputRef(node_id="physical-vad", port="audio"),
        )
    ]
    segment_source = "physical-vad"
    start_source = "physical-vad"
    stop_source = "physical-vad"
    inferencers: dict[tuple[str, int], object] = {
        ("vad-engine", 1): vad,
        ("asr-engine", 1): asr,
    }
    assets = {
        "vad-engine": _asset(
            "vad-engine",
            kind=EngineKind.VAD,
            api=EngineApi.UX_VAD,
        ),
        "asr-engine": _asset(
            "asr-engine",
            kind=EngineKind.ASR,
            api=EngineApi.UX_SPEECH,
        ),
    }
    if semantic_vad is not None:
        nodes.insert(
            1,
            SemanticVadNode(
                id="semantic-vad",
                engine_id="semantic-engine",
            ),
        )
        edges.append(
            AudioEdge(
                id="vad-semantic",
                source=NodeOutputRef(node_id="physical-vad", port="segments"),
                target=NodeInputRef(node_id="semantic-vad", port="segments"),
            )
        )
        segment_source = "semantic-vad"
        stop_source = "semantic-vad"
        inferencers[("semantic-engine", 1)] = semantic_vad
        assets["semantic-engine"] = _asset(
            "semantic-engine",
            kind=EngineKind.VAD,
            api=EngineApi.UX_VAD,
        )
    if speaker_filter:
        nodes.extend(
            [
                AudioEmbeddingNode(
                    id="embedding",
                    engine_id="embedding-engine",
                    config=AudioEmbeddingNodeConfig(
                        algorithm="test",
                        expected_dimension=2,
                    ),
                ),
                VectorSearchNode(
                    id="vector-search",
                    config=VectorSearchNodeConfig(
                        collection_name="voiceprints",
                        vector_name="test",
                    ),
                ),
                SpeakerFilterNode(
                    id="speaker-filter",
                    config=SpeakerFilterNodeConfig(
                        allowed_speaker_ids=["speaker-1"],
                    ),
                ),
            ]
        )
        edges.extend(
            [
                AudioEdge(
                    id="segments-embedding",
                    source=NodeOutputRef(
                        node_id=segment_source,
                        port="segments",
                    ),
                    target=NodeInputRef(
                        node_id="embedding",
                        port="segments",
                    ),
                ),
                AudioEdge(
                    id="embedding-search",
                    source=NodeOutputRef(
                        node_id="embedding",
                        port="embeddings",
                    ),
                    target=NodeInputRef(
                        node_id="vector-search",
                        port="embeddings",
                    ),
                ),
                AudioEdge(
                    id="segments-filter",
                    source=NodeOutputRef(
                        node_id=segment_source,
                        port="segments",
                    ),
                    target=NodeInputRef(
                        node_id="speaker-filter",
                        port="segments",
                    ),
                ),
                AudioEdge(
                    id="search-filter",
                    source=NodeOutputRef(
                        node_id="vector-search",
                        port="results",
                    ),
                    target=NodeInputRef(
                        node_id="speaker-filter",
                        port="results",
                    ),
                ),
                AudioEdge(
                    id="filter-asr",
                    source=NodeOutputRef(
                        node_id="speaker-filter",
                        port="segments",
                    ),
                    target=NodeInputRef(node_id="asr", port="segments"),
                ),
            ]
        )
        inferencers[("embedding-engine", 1)] = object()
        assets["embedding-engine"] = _asset(
            "embedding-engine",
            kind=EngineKind.EMBEDDING,
            api=EngineApi.ENGINE_FEATURE,
        )
        start_source = "speaker-filter"
        stop_source = "speaker-filter"
    else:
        edges.append(
            AudioEdge(
                id="segments-asr",
                source=NodeOutputRef(
                    node_id=segment_source,
                    port="segments",
                ),
                target=NodeInputRef(node_id="asr", port="segments"),
            )
        )
    document = AudioWorkflowDocument(
        nodes=nodes,
        edges=edges,
        outputs=[
            NamedOutputBinding(
                name="primary_transcript",
                source=NodeOutputRef(node_id="asr", port="transcripts"),
            ),
            NamedOutputBinding(
                name="speech_started",
                source=NodeOutputRef(
                    node_id=start_source,
                    port="speech_started",
                ),
            ),
            NamedOutputBinding(
                name="speech_stopped",
                source=NodeOutputRef(node_id=stop_source, port="speech_stopped"),
            ),
            *(
                [
                    NamedOutputBinding(
                        name="turn_progress",
                        source=NodeOutputRef(
                            node_id="semantic-vad",
                            port="turn_progress",
                        ),
                    )
                ]
                if semantic_vad is not None
                else []
            ),
            *(
                [
                    NamedOutputBinding(
                        name="speaker_filter",
                        source=NodeOutputRef(
                            node_id="speaker-filter",
                            port="decisions",
                        ),
                    )
                ]
                if speaker_filter
                else []
            ),
        ],
    )
    workflow = ActiveAudioWorkflow(
        id="default-audio",
        database_revision_id=1,
        revision=1,
        document=document,
        plan=compile_audio_graph(document, assets=assets),
    )
    return (
        StaticInferencerPool(inferencers),
        StaticAudioWorkflowService(workflow),
        workflow,
    )
