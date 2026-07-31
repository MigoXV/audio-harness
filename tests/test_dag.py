from __future__ import annotations

from datetime import datetime, timezone

import pytest

from audio_harness.assets import (
    EngineApi,
    EngineAssetData,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.pipelines.dag import (
    FailurePolicy,
    GraphValidationError,
    PipelineDefinition,
    compile_pipeline,
    default_node_registry,
)


def asset(
    asset_id: str,
    *,
    kind: EngineKind,
    api: EngineApi,
    enabled: bool = True,
) -> EngineAssetData:
    now = datetime.now(timezone.utc)
    return EngineAssetData(
        id=asset_id,
        slug=asset_id,
        name=asset_id,
        kind=kind,
        api=api,
        protocol=EngineProtocol.GRPC,
        target="127.0.0.1:50051",
        connect_timeout_seconds=1,
        request_timeout_seconds=None,
        enabled=enabled,
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


def valid_definition(*, include_semantic: bool = True) -> PipelineDefinition:
    nodes = [
        {"id": "input", "type": "audio_input", "position": {"x": 0, "y": 0}},
        {
            "id": "vad",
            "type": "physical_vad",
            "engine_id": "vad-engine",
            "config": {
                "min_segment_seconds": 0,
                "max_segment_seconds": 60,
                "audio_buffer_seconds": 61,
                "speech_pre_roll_seconds": 0,
            },
        },
        {
            "id": "asr",
            "type": "asr",
            "engine_id": "asr-engine",
        },
        {"id": "output", "type": "session_output"},
    ]
    segment_source = "vad"
    if include_semantic:
        nodes.insert(
            2,
            {
                "id": "semantic",
                "type": "semantic_vad",
                "engine_id": "vad-engine",
                "failure_policy": "fail_open",
            },
        )
        segment_source = "semantic"
    edges = [
        {
            "id": "input-vad",
            "source_node": "input",
            "source_port": "audio",
            "target_node": "vad",
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
            "id": "vad-events-output",
            "source_node": "vad",
            "source_port": "events",
            "target_node": "output",
            "target_port": "events",
        },
        {
            "id": "asr-events-output",
            "source_node": "asr",
            "source_port": "events",
            "target_node": "output",
            "target_port": "events",
        },
    ]
    if include_semantic:
        edges.extend(
            [
                {
                    "id": "vad-semantic",
                    "source_node": "vad",
                    "source_port": "segments",
                    "target_node": "semantic",
                    "target_port": "segments",
                },
                {
                    "id": "semantic-events-output",
                    "source_node": "semantic",
                    "source_port": "events",
                    "target_node": "output",
                    "target_port": "events",
                },
            ]
        )
    return PipelineDefinition.model_validate({"nodes": nodes, "edges": edges})


@pytest.fixture
def assets() -> dict[str, EngineAssetData]:
    return {
        "asr-engine": asset(
            "asr-engine",
            kind=EngineKind.ASR,
            api=EngineApi.UX_SPEECH,
        ),
        "vad-engine": asset(
            "vad-engine",
            kind=EngineKind.VAD,
            api=EngineApi.UX_VAD,
        ),
    }


def test_compiler_builds_stable_plan(
    assets: dict[str, EngineAssetData],
) -> None:
    plan = compile_pipeline(
        valid_definition(),
        registry=default_node_registry(),
        assets=assets,
    )

    assert plan.topological_order == (
        "input",
        "vad",
        "semantic",
        "asr",
        "output",
    )
    assert [item.id for item in plan.engine_snapshots] == [
        "asr-engine",
        "vad-engine",
    ]
    assert (
        plan.nodes["semantic"].failure_policy
        is FailurePolicy.FAIL_OPEN
    )


def test_compiler_rejects_cycle(
    assets: dict[str, EngineAssetData],
) -> None:
    definition = valid_definition()
    raw = definition.model_dump(mode="json")
    raw["edges"].append(
        {
            "id": "semantic-cycle",
            "source_node": "semantic",
            "source_port": "segments",
            "target_node": "semantic",
            "target_port": "segments",
        }
    )

    with pytest.raises(GraphValidationError) as captured:
        compile_pipeline(
            PipelineDefinition.model_validate(raw),
            registry=default_node_registry(),
            assets=assets,
        )

    assert "cycle_detected" in {
        item.code for item in captured.value.diagnostics
    }


def test_compiler_rejects_incompatible_engine(
    assets: dict[str, EngineAssetData],
) -> None:
    raw = valid_definition().model_dump(mode="json")
    next(node for node in raw["nodes"] if node["id"] == "asr")[
        "engine_id"
    ] = "vad-engine"

    with pytest.raises(GraphValidationError) as captured:
        compile_pipeline(
            PipelineDefinition.model_validate(raw),
            registry=default_node_registry(),
            assets=assets,
        )

    assert any(
        item.code == "engine_api_mismatch" and item.node_id == "asr"
        for item in captured.value.diagnostics
    )


def test_compiler_allows_explicit_aggregate_input(
    assets: dict[str, EngineAssetData],
) -> None:
    plan = compile_pipeline(
        valid_definition(),
        registry=default_node_registry(),
        assets=assets,
    )

    incoming = [
        edge
        for edge in plan.edges
        if edge.target_node == "output" and edge.target_port == "events"
    ]
    assert len(incoming) == 3


def test_compiler_rejects_missing_required_input(
    assets: dict[str, EngineAssetData],
) -> None:
    raw = valid_definition().model_dump(mode="json")
    raw["edges"] = [
        edge for edge in raw["edges"] if edge["id"] != "segments-asr"
    ]

    with pytest.raises(GraphValidationError) as captured:
        compile_pipeline(
            PipelineDefinition.model_validate(raw),
            registry=default_node_registry(),
            assets=assets,
        )

    assert any(
        item.code == "required_input_missing" and item.node_id == "asr"
        for item in captured.value.diagnostics
    )
