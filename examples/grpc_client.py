from __future__ import annotations

import argparse
import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import grpc

from audio_harness.protos.asr import ux_speech_pb2 as pb2
from audio_harness.protos.asr import ux_speech_pb2_grpc as pb2_grpc


def duration_seconds(value) -> float:
    return value.seconds + value.nanos / 1_000_000_000


async def request_stream(
    wav_path: Path,
    *,
    chunk_bytes: int,
    language_code: str,
) -> AsyncIterator[pb2.StreamingRecognizeRequest]:
    with wave.open(str(wav_path), "rb") as wav_file:
        if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
            raise ValueError("示例客户端只支持单声道 16-bit PCM WAV")
        sample_rate = wav_file.getframerate()
        yield pb2.StreamingRecognizeRequest(
            streaming_config=pb2.StreamingRecognitionConfig(
                config=pb2.RecognitionConfig(
                    encoding=pb2.RecognitionConfig.LINEAR16,
                    sample_rate_hertz=sample_rate,
                    language_code=language_code,
                    enable_automatic_punctuation=True,
                ),
                interim_results=True,
            )
        )
        frames_per_chunk = max(1, chunk_bytes // 2)
        while audio := wav_file.readframes(frames_per_chunk):
            yield pb2.StreamingRecognizeRequest(audio_content=audio)


async def transcribe(args: argparse.Namespace) -> None:
    async with grpc.aio.insecure_channel(args.target) as channel:
        stub = pb2_grpc.UxSpeechStub(channel)
        responses = stub.StreamingRecognize(
            request_stream(
                args.wav,
                chunk_bytes=args.chunk_bytes,
                language_code=args.language,
            )
        )
        async for response in responses:
            for result in response.results:
                alternative = result.alternative
                word = alternative.words[0] if alternative.words else None
                start = duration_seconds(word.start_time) if word else 0.0
                end = duration_seconds(word.end_time) if word else 0.0
                state = "final" if result.is_final else "interim"
                print(
                    f"[{start:.2f}s - {end:.2f}s] "
                    f"{state} turn_completed={alternative.turn_completed}: "
                    f"{alternative.transcript}",
                    flush=True,
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="调用 Audio Harness gRPC 服务")
    parser.add_argument("wav", type=Path, help="单声道 16-bit PCM WAV 文件")
    parser.add_argument("--target", default="127.0.0.1:50051")
    parser.add_argument("--chunk-bytes", type=int, default=6400)
    parser.add_argument("--language", default="zh-CN")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(transcribe(parse_args()))
