from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from audio_harness.assets import (
    EngineApi,
    EngineAssetData,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.pipelines.dag import (
    PhysicalVadNodeConfig,
    PipelineDefinition,
    compile_pipeline,
    default_node_registry,
)
from audio_harness.pipelines.service import ActivePipelineSnapshot


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
        enabled=True,
        status=EngineStatus.CONFIRMED_RUNNING,
        image=None,
        description=None,
        status_basis=None,
        status_observed_at=None,
        source=None,
        details={},
        revision=1,
        created_at=now,
        updated_at=now,
    )


class StaticEngineRouter:
    def __init__(self, engines: dict[tuple[str, int], object]) -> None:
        self._engines = engines

    @asynccontextmanager
    async def lease_snapshots(self, snapshots):
        keys = {(snapshot.id, snapshot.revision) for snapshot in snapshots}
        yield {key: self._engines[key] for key in keys}


class StaticPipelineStore:
    def __init__(self, snapshot: ActivePipelineSnapshot) -> None:
        self._snapshot = snapshot

    def current(self) -> ActivePipelineSnapshot:
        return self._snapshot

    def physical_vad_config(self) -> PhysicalVadNodeConfig:
        node = self._snapshot.plan.nodes["physical_vad"]
        assert isinstance(node.config, PhysicalVadNodeConfig)
        return node.config


def static_graph(
    *,
    asr: object,
    vad: object,
    semantic_vad: object | None = None,
    physical_config: PhysicalVadNodeConfig | None = None,
) -> tuple[StaticEngineRouter, StaticPipelineStore]:
    config = physical_config or PhysicalVadNodeConfig(
        min_segment_seconds=0,
        max_segment_seconds=60,
        audio_buffer_seconds=61,
        speech_pre_roll_seconds=0,
    )
    nodes = [
        {"id": "audio_input", "type": "audio_input"},
        {
            "id": "physical_vad",
            "type": "physical_vad",
            "engine_id": "vad-engine",
            "config": config.model_dump(),
        },
        {
            "id": "asr",
            "type": "asr",
            "engine_id": "asr-engine",
        },
        {"id": "session_output", "type": "session_output"},
    ]
    segment_source = "physical_vad"
    engines: dict[tuple[str, int], object] = {
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
            2,
            {
                "id": "semantic_vad",
                "type": "semantic_vad",
                "engine_id": "semantic-engine",
                "failure_policy": "fail_open",
            },
        )
        assets["semantic-engine"] = _asset(
            "semantic-engine",
            kind=EngineKind.VAD,
            api=EngineApi.UX_VAD,
        )
        engines[("semantic-engine", 1)] = semantic_vad
        segment_source = "semantic_vad"

    edges = [
        {
            "id": "audio-vad",
            "source_node": "audio_input",
            "source_port": "audio",
            "target_node": "physical_vad",
            "target_port": "audio",
        },
        {
            "id": "segments-asr",
            "source_node": segment_source,
            "source_port": "segments",
            "target_node": "asr",
            "target_port": "segments",
        },
        {
            "id": "vad-output",
            "source_node": "physical_vad",
            "source_port": "events",
            "target_node": "session_output",
            "target_port": "events",
        },
        {
            "id": "asr-output",
            "source_node": "asr",
            "source_port": "events",
            "target_node": "session_output",
            "target_port": "events",
        },
    ]
    if semantic_vad is not None:
        edges.extend(
            [
                {
                    "id": "vad-semantic",
                    "source_node": "physical_vad",
                    "source_port": "segments",
                    "target_node": "semantic_vad",
                    "target_port": "segments",
                },
                {
                    "id": "semantic-output",
                    "source_node": "semantic_vad",
                    "source_port": "events",
                    "target_node": "session_output",
                    "target_port": "events",
                },
            ]
        )
    definition = PipelineDefinition.model_validate(
        {"nodes": nodes, "edges": edges}
    )
    plan = compile_pipeline(
        definition,
        registry=default_node_registry(),
        assets=assets,
    )
    now = datetime.now(timezone.utc)
    snapshot = ActivePipelineSnapshot(
        definition=definition,
        plan=plan,
        revision=1,
        activated_at=now,
    )
    return StaticEngineRouter(engines), StaticPipelineStore(snapshot)
