from __future__ import annotations

import ast
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src" / "audio_harness"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_business_modules_do_not_import_grpc_or_protobuf_adapters() -> None:
    business_files = [
        SOURCE_ROOT / "engines" / "pool.py",
        SOURCE_ROOT / "engines" / "catalog.py",
        SOURCE_ROOT / "errors.py",
        SOURCE_ROOT / "sessions" / "transcription.py",
        *sorted((SOURCE_ROOT / "workflows").rglob("*.py")),
        *sorted((SOURCE_ROOT / "voice_agents").rglob("*.py")),
    ]
    forbidden_prefixes = (
        "grpc",
        "google.protobuf",
        "audio_harness.protos",
        "audio_harness.inferencers.transports",
        "fastapi",
        "openai.types.realtime",
    )

    violations: list[str] = []
    for path in business_files:
        for module in imported_modules(path):
            if module.startswith(forbidden_prefixes):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)} -> {module}"
                )

    assert violations == []


def test_only_shared_errors_live_at_package_root() -> None:
    root_modules = {
        path.name
        for path in SOURCE_ROOT.glob("*.py")
    }
    assert root_modules == {"__init__.py", "errors.py"}
    assert not (SOURCE_ROOT / "domains").exists()


def test_shared_errors_do_not_depend_on_infrastructure() -> None:
    forbidden_prefixes = (
        "audio_harness.api",
        "audio_harness.configs",
        "audio_harness.database",
        "audio_harness.engines",
        "audio_harness.inferencers",
        "audio_harness.schemas",
        "audio_harness.serving",
    )
    assert not any(
        module.startswith(forbidden_prefixes)
        for module in imported_modules(SOURCE_ROOT / "errors.py")
    )


def test_inferencers_are_grouped_by_capability() -> None:
    inferencer_root = SOURCE_ROOT / "inferencers"

    for capability, protocol in (
        ("asr", "grpc.py"),
        ("vad", "grpc.py"),
        ("responses", "http.py"),
        ("tts", "grpc.py"),
        ("diarization", "grpc.py"),
    ):
        assert (inferencer_root / capability / protocol).is_file()

    assert not (inferencer_root / "grpc").exists()
    assert not (inferencer_root / "http").exists()


def test_inferencer_contracts_are_capability_base_classes() -> None:
    inferencer_root = SOURCE_ROOT / "inferencers"

    for capability in ("asr", "vad", "tts", "diarization"):
        assert (inferencer_root / capability / "base.py").is_file()

    assert not (inferencer_root / "base.py").exists()
    assert not (inferencer_root / "responses" / "base.py").exists()
    assert not (inferencer_root / "contracts.py").exists()
    assert not (inferencer_root / "_grpc.py").exists()
    assert not (inferencer_root / "factory.py").exists()
    assert (inferencer_root / "transports" / "grpc.py").is_file()
    assert (SOURCE_ROOT / "engines" / "inferencer_factory.py").is_file()

    pool_source = (
        SOURCE_ROOT / "engines" / "pool.py"
    ).read_text(encoding="utf-8")
    factory_source = (
        SOURCE_ROOT / "engines" / "inferencer_factory.py"
    ).read_text(encoding="utf-8")
    assert "Callable[[EngineSnapshot], Inferencer]" in pool_source
    assert "InferencerFactory" not in factory_source

    for path in inferencer_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert re.search(r"\bProtocol\b", source) is None


def test_inferencer_data_types_are_owned_by_capability_bases() -> None:
    inferencer_root = SOURCE_ROOT / "inferencers"

    asr = (inferencer_root / "asr" / "base.py").read_text(encoding="utf-8")
    vad = (inferencer_root / "vad" / "base.py").read_text(encoding="utf-8")
    tts = (inferencer_root / "tts" / "base.py").read_text(encoding="utf-8")
    assert "class Options(NamedTuple)" in asr
    assert "class Result(NamedTuple)" in asr
    assert "class Update(NamedTuple)" in vad
    assert "class Postprocessing(TypedDict)" in vad
    assert "class ReferenceVoice(NamedTuple)" in tts
    assert "class Chunk(NamedTuple)" in tts


def test_pcm_and_workflow_events_have_explicit_owners() -> None:
    media_pcm = SOURCE_ROOT / "media" / "pcm.py"
    workflow_events = SOURCE_ROOT / "workflows" / "events.py"

    assert media_pcm.is_file()
    assert workflow_events.is_file()
    assert "class PcmChunk(NamedTuple)" in media_pcm.read_text(
        encoding="utf-8"
    )
    event_source = workflow_events.read_text(encoding="utf-8")
    assert "class SpeechStarted(NamedTuple)" in event_source
    assert "class SpeechStopped(NamedTuple)" in event_source

    forbidden_prefixes = (
        "audio_harness.inferencers",
        "audio_harness.serving",
        "audio_harness.workflows",
    )
    assert not any(
        module.startswith(forbidden_prefixes)
        for module in imported_modules(media_pcm)
    )


def test_realtime_routes_have_an_explicit_owner() -> None:
    route_root = SOURCE_ROOT / "realtime_routes"

    assert (route_root / "models.py").is_file()
    assert (route_root / "service.py").is_file()
    assert (SOURCE_ROOT / "schemas" / "realtime_routes.py").is_file()
    assert (SOURCE_ROOT / "api" / "routers" / "realtime_routes.py").is_file()

    voice_agent_service = (
        SOURCE_ROOT / "voice_agents" / "service.py"
    ).read_text(encoding="utf-8")
    workflow_router = (
        SOURCE_ROOT / "api" / "routers" / "workflows.py"
    ).read_text(encoding="utf-8")
    assert "class RealtimeRouteService" not in voice_agent_service
    assert '"/realtime-routes' not in workflow_router


def test_realtime_does_not_implement_wire_codecs() -> None:
    realtime_root = SOURCE_ROOT / "serving" / "realtime"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in realtime_root.glob("*.py")
    )

    assert "decode_wire_audio" not in source
    assert "encode_wire_audio" not in source
    assert "audioop" not in source
    assert "audio/pcmu" not in source
    assert "audio/pcma" not in source


def test_workflow_session_uses_injected_inferencers() -> None:
    session_source = (
        SOURCE_ROOT / "sessions" / "transcription.py"
    ).read_text(encoding="utf-8")
    runtime_source = (
        SOURCE_ROOT / "workflows" / "runtime.py"
    ).read_text(encoding="utf-8")

    session_class = session_source.split(
        "class AudioWorkflowSession:", 1
    )[1].split("class AudioWorkflowSessionService:", 1)[0]
    assert "InferencerPool" not in session_class
    assert "inferencer_pool" not in session_class
    assert "AudioWorkflowInferencer" not in runtime_source
    assert "class AudioWorkflowRuntime:" in runtime_source


def test_capacity_is_owned_by_resource_packages() -> None:
    assert not (SOURCE_ROOT / "capacity").exists()
    assert (SOURCE_ROOT / "sessions" / "limits.py").is_file()
    assert (SOURCE_ROOT / "inferencers" / "limits.py").is_file()
    assert (SOURCE_ROOT / "serving" / "capacity.py").is_file()
    assert (SOURCE_ROOT / "serving" / "capacity_settings.py").is_file()


def test_fastapi_adapters_and_schemas_have_explicit_boundaries() -> None:
    assert (SOURCE_ROOT / "api" / "app.py").is_file()
    assert (SOURCE_ROOT / "api" / "routers").is_dir()
    assert not (SOURCE_ROOT / "serving" / "http").exists()

    violations: list[str] = []
    for path in (SOURCE_ROOT / "schemas").glob("*.py"):
        for module in imported_modules(path):
            if module.startswith("fastapi"):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)} -> {module}"
                )
    assert violations == []
