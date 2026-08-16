from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from audio_harness.configs import AppConfig, DatabaseConfig
from audio_harness.engines.models import EngineApi
from audio_harness.serving.host import AudioHarnessHost


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
API_MARKER = re.compile(
    r"<!--\s*API:\s*(GET|POST|PUT|PATCH|DELETE)\s+([^\s]+)\s*-->"
)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def runtime_openapi() -> dict:
    config = AppConfig.from_environment().model_copy(
        update={"database": DatabaseConfig(url="sqlite+aiosqlite:///:memory:")}
    )
    # 部分 pytest-asyncio 用例结束后会清除主线程默认 loop；GrpcServer 构造
    # 需要一个工作 loop，即使这里只读取 HTTP OpenAPI。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return AudioHarnessHost(config).http_app.openapi()
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def markdown_files() -> list[Path]:
    return sorted(DOCS.rglob("*.md"))


def test_committed_openapi_matches_runtime() -> None:
    expected = runtime_openapi()
    frontend = json.loads((ROOT / "src/web/openapi.json").read_text("utf-8"))
    documentation = json.loads(
        (DOCS / "reference/openapi.json").read_text("utf-8")
    )

    assert frontend == expected
    assert documentation == expected


def test_docs_cover_every_http_operation() -> None:
    schema = runtime_openapi()
    expected = {
        (method.upper(), path)
        for path, path_item in schema["paths"].items()
        for method in path_item
        if method in HTTP_METHODS
    }
    documented: set[tuple[str, str]] = set()
    for path in markdown_files():
        documented.update(API_MARKER.findall(path.read_text("utf-8")))

    assert documented == expected


def test_docs_cover_every_engine_api() -> None:
    content = "\n".join(
        path.read_text("utf-8")
        for path in (
            DOCS / "control-plane/engines.md",
            DOCS / "engine-integration/README.md",
        )
    )

    assert {api.value for api in EngineApi} <= {
        value for value in re.findall(r"`([a-z][a-z0-9_]*)`", content)
    }


def test_internal_markdown_links_exist() -> None:
    missing: list[str] = []
    for source in markdown_files():
        for raw_target in MARKDOWN_LINK.findall(source.read_text("utf-8")):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if (
                not target
                or target.startswith(("#", "http://", "https://", "mailto:"))
            ):
                continue
            relative = target.split("#", 1)[0].split("?", 1)[0]
            if relative and not (source.parent / relative).resolve().exists():
                missing.append(f"{source.relative_to(ROOT)} -> {target}")

    assert not missing, "文档存在失效的内部链接:\n" + "\n".join(missing)
