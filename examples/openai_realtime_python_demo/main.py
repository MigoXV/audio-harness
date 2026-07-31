from __future__ import annotations

import argparse
import asyncio
import base64
import os
import ssl
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr
from openai import AsyncOpenAI
from openai.resources.realtime.realtime import AsyncRealtimeConnection

SAMPLE_RATE = 24_000
CHUNK_SAMPLES = 2_400
TRAILING_SILENCE_SECONDS = 3


def load_wav(path: Path, seconds: float | None) -> bytes:
    audio, source_rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if seconds is not None:
        audio = audio[: round(seconds * source_rate)]
    if source_rate != SAMPLE_RATE:
        audio = soxr.resample(audio, source_rate, SAMPLE_RATE)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    return pcm.tobytes()


async def send_audio(
    connection: AsyncRealtimeConnection,
    pcm: bytes,
) -> None:
    pcm += b"\0" * (
        SAMPLE_RATE * 2 * TRAILING_SILENCE_SECONDS
    )
    chunk_bytes = CHUNK_SAMPLES * 2

    for offset in range(0, len(pcm), chunk_bytes):
        chunk = pcm[offset : offset + chunk_bytes]
        await connection.input_audio_buffer.append(
            audio=base64.b64encode(chunk).decode("ascii")
        )


async def receive_results(
    connection: AsyncRealtimeConnection,
    sender: asyncio.Task[None],
) -> None:
    loop = asyncio.get_running_loop()
    last_event_at = loop.time()

    while True:
        try:
            event = await asyncio.wait_for(
                connection.recv(),
                timeout=0.5,
            )
        except asyncio.TimeoutError:
            if sender.done() and loop.time() - last_event_at >= 3:
                return
            continue

        last_event_at = loop.time()
        if event.type == (
            "conversation.item.input_audio_transcription.completed"
        ):
            print(event.transcript)
        elif event.type == "error":
            raise RuntimeError(event.error.message)


async def run(path: Path, seconds: float | None) -> None:
    api_key = os.environ["OPENAI_API_KEY"]
    base_url = os.getenv(
        "OPENAI_BASE_URL",
        "https://api.openai.com/v1",
    )
    model = os.getenv("OPENAI_MODEL", "gpt-4o-transcribe")
    ca_file = os.getenv("OPENAI_CA_CERT_FILE")
    websocket_options = (
        {"ssl": ssl.create_default_context(cafile=ca_file)}
        if ca_file
        else {}
    )

    pcm = await asyncio.to_thread(load_wav, path, seconds)
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    async with client.realtime.connect(
        model=model,
        websocket_connection_options=websocket_options,  # type: ignore[arg-type]
    ) as connection:
        await connection.session.update(
            session={
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": SAMPLE_RATE,
                        },
                        "transcription": {
                            "model": model,
                            "language": "zh",
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "create_response": False,
                        },
                    }
                },
            }
        )

        while True:
            event = await connection.recv()
            if event.type == "session.updated":
                break
            if event.type == "error":
                raise RuntimeError(event.error.message)

        sender = asyncio.create_task(send_audio(connection, pcm))
        try:
            await receive_results(connection, sender)
            await sender
        finally:
            if not sender.done():
                sender.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把 WAV 按实时速度流式发送到 OpenAI Realtime API"
    )
    parser.add_argument("wav", type=Path)
    parser.add_argument(
        "--seconds",
        type=float,
        help="只发送 WAV 的前 N 秒",
    )
    args = parser.parse_args()
    asyncio.run(run(args.wav, args.seconds))


if __name__ == "__main__":
    main()
