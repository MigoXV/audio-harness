#!/usr/bin/env python3
"""导出 OpenAPI，供前端和接口文档共享同一份机器契约。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from audio_harness.configs.app import AppConfig, DatabaseConfig
from audio_harness.serving.host import AudioHarnessHost


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "src/web/openapi.json"
DOCUMENTATION_OUTPUT = REPOSITORY_ROOT / "docs/reference/openapi.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "OpenAPI JSON 输出路径。未指定时同时更新前端契约和 docs 契约。"
        ),
    )
    return parser


def build_openapi_json() -> str:
    """从真实组合根生成格式稳定的 OpenAPI JSON。"""

    config = AppConfig.from_environment().model_copy(
        update={"database": DatabaseConfig(url="sqlite+aiosqlite:///:memory:")}
    )
    server = AudioHarnessHost(config)
    return json.dumps(
        server.http_app.openapi(),
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def write_openapi(output: Path, content: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    print(f"已导出 OpenAPI: {output}")


def main() -> int:
    args = build_parser().parse_args()
    content = build_openapi_json()
    outputs = (
        (args.output,)
        if args.output is not None
        else (DEFAULT_OUTPUT, DOCUMENTATION_OUTPUT)
    )
    for output in outputs:
        write_openapi(output, content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
