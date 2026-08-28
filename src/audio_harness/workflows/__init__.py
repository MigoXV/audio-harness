from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AutomaticGainControlConfig": "audio_harness.workflows.models",
    "AutomaticGainControlNode": "audio_harness.workflows.models",
    "AsrNode": "audio_harness.workflows.models",
    "AsrNodeConfig": "audio_harness.workflows.models",
    "AudioEdge": "audio_harness.workflows.models",
    "AudioGraphDiagnostic": "audio_harness.workflows.compiler",
    "AudioGraphExecutionError": "audio_harness.workflows.runtime",
    "AudioGraphValidationError": "audio_harness.workflows.compiler",
    "AudioInferenceRequest": "audio_harness.workflows.runtime",
    "AudioRuntimeConfig": "audio_harness.workflows.runtime",
    "AudioWorkflowDocument": "audio_harness.workflows.models",
    "AudioWorkflowOutput": "audio_harness.workflows.runtime",
    "AudioWorkflowRepository": "audio_harness.workflows.repository",
    "AudioWorkflowRuntime": "audio_harness.workflows.runtime",
    "AudioWorkflowService": "audio_harness.workflows.service",
    "CompiledAudioGraph": "audio_harness.workflows.compiler",
    "FailurePolicy": "audio_harness.workflows.models",
    "GraphInputRef": "audio_harness.workflows.models",
    "HdbscanClusteringParameters": "audio_harness.workflows.models",
    "NamedOutputBinding": "audio_harness.workflows.models",
    "NodeInputRef": "audio_harness.workflows.models",
    "NodeOutputRef": "audio_harness.workflows.models",
    "NodePosition": "audio_harness.workflows.models",
    "PhysicalVadConfig": "audio_harness.workflows.models",
    "PhysicalVadNode": "audio_harness.workflows.models",
    "SemanticVadConfig": "audio_harness.workflows.models",
    "SemanticVadNode": "audio_harness.workflows.models",
    "SpeakerClusteringNode": "audio_harness.workflows.models",
    "SpeakerClusteringNodeConfig": "audio_harness.workflows.models",
    "SpeakerClusteringPostprocessingOptions": "audio_harness.workflows.models",
    "SpeakerDiarizationNode": "audio_harness.workflows.models",
    "SpeakerDiarizationNodeConfig": "audio_harness.workflows.models",
    "SpeakerDiarizationPostprocessingOptions": "audio_harness.workflows.models",
    "SpeakerFilterNode": "audio_harness.workflows.models",
    "SpeakerFilterNodeConfig": "audio_harness.workflows.models",
    "StreamingDenoiseConfig": "audio_harness.workflows.models",
    "StreamingDenoiseNode": "audio_harness.workflows.models",
    "audio_node_catalog": "audio_harness.workflows.compiler",
    "compile_audio_graph": "audio_harness.workflows.compiler",
    "compiled_audio_graph_document": "audio_harness.workflows.compiler",
    "default_audio_workflow": "audio_harness.workflows.models",
    "restore_audio_graph": "audio_harness.workflows.compiler",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
