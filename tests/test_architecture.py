from __future__ import annotations

import ast
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
        SOURCE_ROOT / "domain.py",
        SOURCE_ROOT / "engines" / "pool.py",
        SOURCE_ROOT / "api" / "service.py",
        SOURCE_ROOT / "sessions" / "transcription.py",
        *sorted((SOURCE_ROOT / "workflows").rglob("*.py")),
        *sorted((SOURCE_ROOT / "voice_agents").rglob("*.py")),
    ]
    forbidden_prefixes = (
        "grpc",
        "google.protobuf",
        "audio_harness.protos",
        "audio_harness.inferencers.grpc",
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
