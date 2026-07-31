from __future__ import annotations

import asyncio
import fractions
import time

from aiortc import AudioStreamTrack, MediaStreamError, MediaStreamTrack
from av import AudioFrame
from av.audio.resampler import AudioResampler

from audio_harness.serving.realtime.session import ENGINE_SAMPLE_RATE

_OUTPUT_SAMPLE_RATE = 48_000
_OUTPUT_FRAME_SAMPLES = 960


class ServerAudioTrack(AudioStreamTrack):
    """预留的服务端输出音轨；队列为空时按 20 ms 输出静音。"""

    def __init__(self, *, queue_capacity: int = 50) -> None:
        super().__init__()
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._start: float | None = None
        self._timestamp = 0

    async def enqueue(self, frame: AudioFrame) -> None:
        await self._queue.put(frame)

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        if self._start is None:
            self._start = time.monotonic()
        else:
            self._timestamp += _OUTPUT_FRAME_SAMPLES
            target = self._start + self._timestamp / _OUTPUT_SAMPLE_RATE
            await asyncio.sleep(max(0, target - time.monotonic()))
        try:
            frame = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            frame = AudioFrame(
                format="s16",
                layout="mono",
                samples=_OUTPUT_FRAME_SAMPLES,
            )
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
        frame.pts = self._timestamp
        frame.sample_rate = _OUTPUT_SAMPLE_RATE
        frame.time_base = fractions.Fraction(1, _OUTPUT_SAMPLE_RATE)
        return frame


class IncomingAudioPump:
    def __init__(
        self,
        *,
        track: MediaStreamTrack,
        chunk_bytes: int,
        append_audio,
    ) -> None:
        self._track = track
        self._chunk_bytes = chunk_bytes
        self._append_audio = append_audio
        self._resampler = AudioResampler(
            format="s16",
            layout="mono",
            rate=ENGINE_SAMPLE_RATE,
        )
        self._buffer = bytearray()

    async def run(self) -> None:
        try:
            while True:
                frame = await self._track.recv()
                for converted in self._resampler.resample(frame):
                    self._buffer.extend(
                        converted.to_ndarray().astype("<i2", copy=False).tobytes()
                    )
                    await self._drain(flush=False)
        except MediaStreamError:
            for converted in self._resampler.resample(None):
                self._buffer.extend(
                    converted.to_ndarray().astype("<i2", copy=False).tobytes()
                )
            await self._drain(flush=True)

    async def _drain(self, *, flush: bool) -> None:
        while len(self._buffer) >= self._chunk_bytes:
            chunk = bytes(self._buffer[: self._chunk_bytes])
            del self._buffer[: self._chunk_bytes]
            await self._append_audio(chunk)
        if flush and self._buffer:
            chunk = bytes(self._buffer)
            self._buffer.clear()
            await self._append_audio(chunk)
