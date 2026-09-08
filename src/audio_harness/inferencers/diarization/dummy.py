from __future__ import annotations

from collections.abc import AsyncIterator

from audio_harness.inferencers.diarization.base import DiarizationInferencer


class DummyDiarizationInferencer(DiarizationInferencer):
    """把每次输入的完整 PCM 批次视为同一个说话人。"""

    async def ready(self) -> None:
        return None

    async def probe(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def stream_diarize(
        self,
        audio: bytes,
        sample_rate: int,
    ) -> AsyncIterator[tuple[DiarizationInferencer.Segment, ...]]:
        if not audio or len(audio) % 2:
            raise ValueError("diarization 输入必须是非空 PCM S16LE")
        if sample_rate <= 0:
            raise ValueError("diarization 采样率必须大于 0")
        yield (
            DiarizationInferencer.Segment(
                start_sample=0,
                end_sample=len(audio) // 2,
                speaker=0,
            ),
        )
