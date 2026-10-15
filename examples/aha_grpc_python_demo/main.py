"""aha-grpc TLS 转写及语音智能体示例。"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import wave

import grpc

from audio_harness.serving.aha_grpc.client import AhaGrpcClient

logger = logging.getLogger(__name__)


async def run(args: argparse.Namespace) -> None:
    rate = 24_000
    pcm = b""
    if args.audio is not None:
        with wave.open(str(args.audio), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() not in (16_000, 24_000)
            ):
                raise ValueError("输入 WAV 必须为单声道 16-bit PCM，采样率为 16/24 kHz")
            rate = source.getframerate()
            pcm = source.readframes(source.getnframes())
    credentials = grpc.ssl_channel_credentials(args.ca_cert.read_bytes())
    async with grpc.aio.secure_channel(args.target, credentials) as channel:
        client = AhaGrpcClient(channel)
        connection = client.connect(
            intent="transcription" if args.mode == "transcription" else None,
            model=args.model if args.mode == "agent" else None,
            timeout=args.timeout,
        )
        audio = bytearray()
        configured = asyncio.Event()
        async with connection:

            async def receive() -> None:
                async for event in connection.events():
                    kind = event["type"]
                    if kind == "session.updated":
                        configured.set()
                    if kind == "response.output_audio.delta":
                        audio.extend(event["delta"])
                    elif kind == "error":
                        raise RuntimeError(str(event["error"]))
                    else:
                        logger.info(
                            "%s %s",
                            kind,
                            event.get("transcript", event.get("delta", "")),
                        )

            async def send() -> None:
                if args.mode == "transcription":
                    await connection.send(
                        {
                            "type": "session.update",
                            "session": {
                                "type": "transcription",
                                "audio": {
                                    "input": {
                                        "format": {"type": "audio/pcm", "rate": rate},
                                        "transcription": {"model": args.model},
                                        "turn_detection": None,
                                    }
                                },
                            },
                        }
                    )
                    await configured.wait()
                    chunk_bytes = rate // 50 * 2  # 20 ms PCM chunks.
                    for offset in range(0, len(pcm), chunk_bytes):
                        await connection.send(
                            {
                                "type": "input_audio_buffer.append",
                                "audio": pcm[offset : offset + chunk_bytes],
                            }
                        )
                else:
                    await connection.send(
                        {
                            "type": "response.create",
                            "response": {
                                "input": [
                                    {
                                        "type": "message",
                                        "role": "user",
                                        "content": [
                                            {"type": "input_text", "text": args.text}
                                        ],
                                    }
                                ],
                            },
                        }
                    )
                # The server commits pending audio and drains final events.
                await connection.finish_input()

            tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        if audio:
            with wave.open(str(args.output), "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(24_000)
                target.writeframes(audio)
            logger.info("语音已保存到 %s", args.output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="localhost:50051")
    parser.add_argument(
        "--ca-cert", type=Path, default=Path("model-bin/realtime-tls/cert.pem")
    )
    parser.add_argument(
        "--model", required=True, help="已发布工作流或语音智能体的 Slug"
    )
    parser.add_argument(
        "--mode", choices=("transcription", "agent"), default="transcription"
    )
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--text", default="你好，请介绍一下你自己。")
    parser.add_argument("--output", type=Path, default=Path("aha-grpc-response.wav"))
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if args.mode == "transcription" and args.audio is None:
        parser.error("转写模式必须提供 --audio WAV 文件")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
