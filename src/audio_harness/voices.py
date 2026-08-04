from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import numpy as np
import soundfile as sf
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_harness.database.models import VoiceAsset, utc_now
from audio_harness.domain import PresetVoice, ReferenceVoice, VoiceSelection


class VoiceAssetNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class VoiceAssetSnapshot:
    id: str
    name: str
    ref_text: str | None
    sample_rate: int
    duration_seconds: float
    created_at: datetime


class VoiceAssetStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        storage_dir: Path,
        max_upload_bytes: int,
    ) -> None:
        self._session_factory = session_factory
        self._storage_dir = storage_dir.resolve()
        self._max_upload_bytes = max_upload_bytes
        self._lock = asyncio.Lock()
        self._cache: dict[str, ReferenceVoice] = {}

    @property
    def max_upload_bytes(self) -> int:
        return self._max_upload_bytes

    @staticmethod
    def _snapshot(model: VoiceAsset) -> VoiceAssetSnapshot:
        return VoiceAssetSnapshot(
            id=model.id,
            name=model.name,
            ref_text=model.ref_text,
            sample_rate=model.sample_rate,
            duration_seconds=model.duration_seconds,
            created_at=model.created_at,
        )

    async def list(self) -> tuple[VoiceAssetSnapshot, ...]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(VoiceAsset).order_by(
                    VoiceAsset.created_at,
                    VoiceAsset.id,
                )
            )
            return tuple(self._snapshot(item) for item in result.scalars())

    async def create(
        self,
        *,
        name: str,
        wav: bytes,
        ref_text: str | None,
    ) -> VoiceAssetSnapshot:
        if not name.strip():
            raise ValueError("音色名称不能为空")
        if not wav or len(wav) > self._max_upload_bytes:
            raise ValueError("参考 WAV 为空或超过上传大小限制")
        try:
            samples, sample_rate = sf.read(
                io.BytesIO(wav),
                dtype="int16",
                always_2d=True,
            )
        except Exception as exc:
            raise ValueError(f"无法读取参考 WAV: {exc}") from exc
        if sample_rate <= 0 or samples.shape[0] == 0:
            raise ValueError("参考 WAV 没有有效音频")
        if samples.shape[1] > 1:
            samples = np.mean(
                samples.astype(np.int32),
                axis=1,
            ).clip(-32768, 32767).astype(np.int16).reshape(-1, 1)
        voice_id = f"voice_{uuid4().hex}"
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        path = (self._storage_dir / f"{voice_id}.wav").resolve()
        path.relative_to(self._storage_dir)
        sf.write(path, samples[:, 0], sample_rate, subtype="PCM_16")
        now = utc_now()
        model = VoiceAsset(
            id=voice_id,
            name=name.strip(),
            wav_path=str(path),
            ref_text=ref_text.strip() if ref_text else None,
            sample_rate=sample_rate,
            duration_seconds=samples.shape[0] / sample_rate,
            created_at=now,
        )
        try:
            async with self._lock:
                async with self._session_factory() as session:
                    session.add(model)
                    await session.commit()
                    await session.refresh(model)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self._cache[voice_id] = ReferenceVoice(
            pcm_s16le=samples[:, 0].astype("<i2", copy=False).tobytes(),
            sample_rate=sample_rate,
            ref_text=model.ref_text or "",
        )
        return self._snapshot(model)

    async def delete(self, voice_id: str) -> None:
        async with self._lock:
            async with self._session_factory() as session:
                model = await session.get(VoiceAsset, voice_id)
                if model is None:
                    raise VoiceAssetNotFoundError(voice_id)
                path = Path(model.wav_path)
                await session.delete(model)
                await session.commit()
            path.unlink(missing_ok=True)
            self._cache.pop(voice_id, None)

    async def resolve(self, voice_id: str) -> VoiceSelection:
        if not voice_id.startswith("voice_"):
            return PresetVoice(voice_id)
        cached = self._cache.get(voice_id)
        if cached is not None:
            return cached
        async with self._session_factory() as session:
            model = await session.get(VoiceAsset, voice_id)
            if model is None:
                raise VoiceAssetNotFoundError(voice_id)
            path = Path(model.wav_path).resolve()
            path.relative_to(self._storage_dir)
            samples, sample_rate = sf.read(
                path,
                dtype="int16",
                always_2d=True,
            )
            selection = ReferenceVoice(
                pcm_s16le=samples[:, 0].astype("<i2", copy=False).tobytes(),
                sample_rate=sample_rate,
                ref_text=model.ref_text or "",
            )
            self._cache[voice_id] = selection
            return selection
