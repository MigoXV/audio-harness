from __future__ import annotations

import logging

import typer

from audio_harness.commands.serve import serve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

app = typer.Typer(add_completion=False, help="Audio Harness 服务管理命令。")


app.command("serve")(serve)


if __name__ == "__main__":
    app()
