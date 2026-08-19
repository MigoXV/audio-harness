from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
from typing import BinaryIO
from uuid import uuid4
import wave


logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16_000
_SAMPLE_WIDTH_BYTES = 2
_CHANNELS = 1
_QUEUE_BLOCK_CAPACITY = 4
_STOP = object()


def _safe_session_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return safe[:100] or "session"


class _SegmentedWavWriter:
    def __init__(
        self,
        *,
        root: Path,
        session_id: str,
        blocks_per_file: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.directory = (
            root
            / now.date().isoformat()
            / _safe_session_id(session_id)
            / uuid4().hex
        )
        self._blocks_per_file = blocks_per_file
        self._sequence = 0
        self._blocks_written = 0
        self._file: BinaryIO | None = None
        self._wave: wave.Wave_write | None = None
        self._part_path: Path | None = None
        self._final_path: Path | None = None

    def write(self, block: bytes) -> None:
        if not block:
            return
        if self._wave is None:
            self._open()
        assert self._wave is not None
        assert self._file is not None
        self._wave.writeframes(block)
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

    def _open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        name = f"{started_at}_{self._sequence:06d}.wav"
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
        self._queue: asyncio.Queue[bytes | object] = asyncio.Queue()
        self._queued_blocks = 0
        self._writer = _SegmentedWavWriter(
            root=root,
            session_id=session_id,
            blocks_per_file=file_seconds // block_seconds,
        )
        self._worker: asyncio.Task[None] | None = None
        self._accepting = True
        self._stop_queued = False
        self._closed = False

    @property
    def directory(self) -> Path:
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
        self._buffer.extend(audio)
        while len(self._buffer) >= self._block_bytes:
            block = bytes(self._buffer[: self._block_bytes])
            del self._buffer[: self._block_bytes]
            if not self._enqueue(block):
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
            if not self._enqueue(bytes(self._buffer)):
                self._disable("关闭时后台写入队列已满")
        self._buffer.clear()
        self._queue_stop()
        if self._worker is not None:
            await self._worker

    def _ensure_worker(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(
                self._run(),
                name=f"received-audio-cache-{self._session_id}",
            )

    def _enqueue(self, block: bytes) -> bool:
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
                    assert isinstance(item, bytes)
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
