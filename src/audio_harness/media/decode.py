from __future__ import annotations

import asyncio
from io import BytesIO

import av
from av.audio.resampler import AudioResampler


def _decode(audio: bytes, max_seconds: int) -> bytes:
    output = bytearray()
    with av.open(BytesIO(audio)) as container:
        streams = list(container.streams.audio)
        if not streams:
            raise ValueError("文件不包含音频轨道")
        resampler = AudioResampler(format="s16", layout="mono", rate=16_000)
        for frame in container.decode(streams[0]):
            for converted in resampler.resample(frame):
                output.extend(converted.to_ndarray().tobytes())
        for converted in resampler.resample(None):
            output.extend(converted.to_ndarray().tobytes())
    if not output:
        raise ValueError("音频为空")
    if len(output) > max_seconds * 16_000 * 2:
        raise ValueError(f"音频超过 {max_seconds} 秒限制")
    return bytes(output)


async def decode_audio(audio: bytes, *, max_bytes: int = 20 * 1024 * 1024, max_seconds: int = 60) -> bytes:
    if not audio:
        raise ValueError("音频为空")
    if len(audio) > max_bytes:
        raise ValueError("音频文件超过 20 MB 限制")
    try:
        return await asyncio.wait_for(asyncio.to_thread(_decode, audio, max_seconds), timeout=30)
    except asyncio.TimeoutError as exc:
        raise ValueError("音频解码超时") from exc
