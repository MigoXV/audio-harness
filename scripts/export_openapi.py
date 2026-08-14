#!/usr/bin/env python3
"""导出控制面 OpenAPI，供前端生成唯一可信的接口类型。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from audio_harness.configs import AppConfig, DatabaseConfig
from audio_harness.serving.host import AudioHarnessHost


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "src/web/openapi.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="OpenAPI JSON 输出路径。",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = AppConfig.from_environment().model_copy(
        update={"database": DatabaseConfig(url="sqlite+aiosqlite:///:memory:")}
    )
    server = AudioHarnessHost(config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(server.http_app.openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已导出 OpenAPI: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
