from __future__ import annotations

import pytest

from audio_harness.inferencers.diarization import DummyDiarizationInferencer


@pytest.mark.asyncio
async def test_dummy_diarization_covers_complete_pcm_with_speaker_zero() -> None:
    inferencer = DummyDiarizationInferencer()

    await inferencer.ready()
    await inferencer.probe()
    results = [
        result
        async for result in inferencer.stream_diarize(b"\x01\x00" * 8, 16_000)
    ]
    await inferencer.close()

    assert results == [((0, 8, 0),)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audio", "sample_rate", "message"),
    [
        (b"", 16_000, "非空 PCM S16LE"),
        (b"\x00", 16_000, "非空 PCM S16LE"),
        (b"\x00\x00", 0, "采样率必须大于 0"),
    ],
)
async def test_dummy_diarization_rejects_invalid_input(
    audio: bytes,
    sample_rate: int,
    message: str,
) -> None:
    inferencer = DummyDiarizationInferencer()

    with pytest.raises(ValueError, match=message):
        await anext(inferencer.stream_diarize(audio, sample_rate))
