from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
from typing import BinaryIO
import wave


logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16_000
_SAMPLE_WIDTH_BYTES = 2
_CHANNELS = 1
_QUEUE_BLOCK_CAPACITY = 4
_STOP = object()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_session_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return safe[:100] or "session"


def _short_session_id(value: str) -> str:
    safe = _safe_session_id(value)
    if safe.startswith("sess_"):
        return f"sess-{safe.removeprefix('sess_')[:8]}"
    return f"session-{safe[:8]}"


def _timestamp_name(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S") + f".{value.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class _TimedAudio:
    data: bytes
    received_at: datetime


class _SegmentedWavWriter:
    def __init__(
        self,
        *,
        root: Path,
        session_id: str,
        block_seconds: int,
        file_seconds: int,
        blocks_per_file: int,
    ) -> None:
        self._root = root
        self._session_id = session_id
        self._block_seconds = block_seconds
        self._file_seconds = file_seconds
        self.directory: Path | None = None
        self._blocks_per_file = blocks_per_file
        self._sequence = 0
        self._blocks_written = 0
        self._file: BinaryIO | None = None
        self._wave: wave.Wave_write | None = None
        self._part_path: Path | None = None
        self._final_path: Path | None = None

    def write(self, timed_audio: _TimedAudio) -> None:
        if not timed_audio.data:
            return
        if self._wave is None:
            self._open(timed_audio.received_at)
        assert self._wave is not None
        assert self._file is not None
        self._wave.writeframes(timed_audio.data)
        self._file.flush()
        self._blocks_written += 1
        if self._blocks_written >= self._blocks_per_file:
            self._publish()

    def close(self) -> None:
        if self._wave is not None:
            self._publish()

    def abandon(self) -> None:
        """关闭失败分片但保留 .part，不把它发布成完整缓存。"""
        try:
            if self._wave is not None:
                self._wave.close()
        finally:
            if self._file is not None:
                self._file.close()
            self._wave = None
            self._file = None

    def _open(self, started_at: datetime) -> None:
        if self.directory is None:
            self.directory = self._create_directory(started_at)
        name = f"{_timestamp_name(started_at)}_{self._sequence:06d}.wav"
        self._final_path = self.directory / name
        self._part_path = self.directory / f"{name}.part"
        descriptor = os.open(
            self._part_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        self._file = os.fdopen(descriptor, "wb")
        self._wave = wave.open(self._file, "wb")
        self._wave.setnchannels(_CHANNELS)
        self._wave.setsampwidth(_SAMPLE_WIDTH_BYTES)
        self._wave.setframerate(_SAMPLE_RATE)
        self._blocks_written = 0

    def _create_directory(self, started_at: datetime) -> Path:
        parent = self._root / started_at.date().isoformat()
        base_name = (
            f"{_timestamp_name(started_at)}__{_short_session_id(self._session_id)}"
        )
        for suffix in range(1, 10_000):
            name = base_name if suffix == 1 else f"{base_name}__{suffix:02d}"
            directory = parent / name
            try:
                directory.mkdir(parents=True, mode=0o700)
            except FileExistsError:
                continue
            directory.chmod(0o700)
            self._write_metadata(directory, started_at)
            return directory
        raise FileExistsError(f"无法创建唯一缓存目录: {parent / base_name}")

    def _write_metadata(self, directory: Path, started_at: datetime) -> None:
        metadata_path = directory / "metadata.json"
        descriptor = os.open(
            metadata_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as metadata_file:
            json.dump(
                {
                    "schema_version": 1,
                    "session_id": self._session_id,
                    "started_at": started_at.isoformat().replace("+00:00", "Z"),
                    "audio_format": {
                        "sample_rate_hz": _SAMPLE_RATE,
                        "channels": _CHANNELS,
                        "sample_format": "pcm_s16le",
                    },
                    "block_seconds": self._block_seconds,
                    "file_seconds": self._file_seconds,
                },
                metadata_file,
                ensure_ascii=False,
                indent=2,
            )
            metadata_file.write("\n")

    def _publish(self) -> None:
        assert self._wave is not None
        assert self._file is not None
        assert self._part_path is not None
        assert self._final_path is not None
        self._wave.close()
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        if self._final_path.exists():
            raise FileExistsError(f"缓存目标文件已经存在: {self._final_path}")
        self._part_path.rename(self._final_path)
        logger.info("接收音频缓存已写入: path=%s", self._final_path)
        self._wave = None
        self._file = None
        self._part_path = None
        self._final_path = None
        self._blocks_written = 0
        self._sequence += 1


class ReceivedAudioCache:
    """按固定块缓存服务端实际处理的 16 kHz PCM 输入。"""

    def __init__(
        self,
        session_id: str,
        *,
        block_seconds: int,
        file_seconds: int,
        root: Path = Path("data-bin/cache"),
    ) -> None:
        if block_seconds <= 0:
            raise ValueError("音频缓存块周期必须大于 0")
        if file_seconds < block_seconds or file_seconds % block_seconds:
            raise ValueError("音频缓存文件周期必须是块周期的正整数倍")
        self._session_id = session_id
        self._block_bytes = (
            _SAMPLE_RATE * _SAMPLE_WIDTH_BYTES * block_seconds
        )
        self._buffer = bytearray()
        self._buffer_chunks: deque[_TimedAudio] = deque()
        self._queue: asyncio.Queue[_TimedAudio | object] = asyncio.Queue()
        self._queued_blocks = 0
        self._writer = _SegmentedWavWriter(
            root=root,
            session_id=session_id,
            block_seconds=block_seconds,
            file_seconds=file_seconds,
            blocks_per_file=file_seconds // block_seconds,
        )
        self._worker: asyncio.Task[None] | None = None
        self._accepting = True
        self._stop_queued = False
        self._closed = False

    @property
    def directory(self) -> Path | None:
        return self._writer.directory

    @property
    def accepting(self) -> bool:
        return self._accepting

    def append(self, audio: bytes) -> None:
        if self._closed or not self._accepting or not audio:
            return
        if len(audio) % _SAMPLE_WIDTH_BYTES:
            raise ValueError("PCM S16LE 音频字节数必须为偶数")
        self._ensure_worker()
        self._buffer_chunks.append(_TimedAudio(audio, _utc_now()))
        self._buffer.extend(audio)
        while len(self._buffer) >= self._block_bytes:
            block = bytes(self._buffer[: self._block_bytes])
            del self._buffer[: self._block_bytes]
            if not self._enqueue(_TimedAudio(block, self._consume_timestamp(len(block)))):
                self._disable("后台写入队列已满")
                return

    async def close(self) -> None:
        if self._closed:
            if self._worker is not None:
                await self._worker
            return
        self._closed = True
        if self._accepting and self._buffer:
            self._ensure_worker()
            audio = bytes(self._buffer)
            if not self._enqueue(
                _TimedAudio(audio, self._consume_timestamp(len(audio)))
            ):
                self._disable("关闭时后台写入队列已满")
        self._buffer.clear()
        self._buffer_chunks.clear()
        self._queue_stop()
        if self._worker is not None:
            await self._worker

    def _ensure_worker(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(
                self._run(),
                name=f"received-audio-cache-{self._session_id}",
            )

    def _consume_timestamp(self, size: int) -> datetime:
        assert self._buffer_chunks
        received_at = self._buffer_chunks[0].received_at
        remaining = size
        while remaining:
            chunk = self._buffer_chunks[0]
            if len(chunk.data) <= remaining:
                self._buffer_chunks.popleft()
                remaining -= len(chunk.data)
                continue
            self._buffer_chunks[0] = _TimedAudio(
                chunk.data[remaining:], chunk.received_at
            )
            remaining = 0
        return received_at

    def _enqueue(self, block: _TimedAudio) -> bool:
        if self._queued_blocks >= _QUEUE_BLOCK_CAPACITY:
            return False
        self._queue.put_nowait(block)
        self._queued_blocks += 1
        return True

    def _disable(self, reason: str) -> None:
        if not self._accepting:
            return
        self._accepting = False
        self._buffer.clear()
        self._buffer_chunks.clear()
        logger.error(
            "接收音频缓存已停用: session_id=%s reason=%s",
            self._session_id,
            reason,
        )
        self._queue_stop()

    def _queue_stop(self) -> None:
        if self._worker is None or self._stop_queued:
            return
        self._stop_queued = True
        self._queue.put_nowait(_STOP)

    async def _run(self) -> None:
        completed = False
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item is _STOP:
                        completed = True
                        return
                    self._queued_blocks -= 1
                    assert isinstance(item, _TimedAudio)
                    await asyncio.to_thread(self._writer.write, item)
                finally:
                    self._queue.task_done()
        except Exception:
            self._accepting = False
            logger.exception(
                "接收音频缓存写入失败，实时会话继续: session_id=%s",
                self._session_id,
            )
        finally:
            self._accepting = False
            self._buffer.clear()
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
            try:
                operation = (
                    self._writer.close
                    if completed
                    else self._writer.abandon
                )
                await asyncio.to_thread(operation)
            except Exception:
                logger.exception(
                    "接收音频缓存关闭失败: session_id=%s",
                    self._session_id,
                )
