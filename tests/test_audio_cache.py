from __future__ import annotations

import wave

import pytest

from audio_harness.media.audio_cache import ReceivedAudioCache


PCM_BYTES_PER_SECOND = 16_000 * 2


def read_wav(path) -> bytes:
    with wave.open(str(path), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == 16_000
        return audio.readframes(audio.getnframes())


@pytest.mark.asyncio
async def test_received_audio_cache_rotates_files_and_flushes_tail(
    tmp_path,
) -> None:
    cache = ReceivedAudioCache(
        "sess/test",
        block_seconds=1,
        file_seconds=2,
        root=tmp_path,
    )
    audio = bytes(
        index % 251
        for index in range(PCM_BYTES_PER_SECOND * 2 + 4_000)
    )

    cache.append(audio[:10_000])
    cache.append(audio[10_000:])
    await cache.close()
    await cache.close()

    paths = sorted(cache.directory.glob("*.wav"))
    assert len(paths) == 2
    assert read_wav(paths[0]) == audio[: PCM_BYTES_PER_SECOND * 2]
    assert read_wav(paths[1]) == audio[PCM_BYTES_PER_SECOND * 2 :]
    assert not list(cache.directory.glob("*.part"))
    assert cache.directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)


@pytest.mark.asyncio
async def test_received_audio_cache_does_not_create_empty_directory(
    tmp_path,
) -> None:
    cache = ReceivedAudioCache(
        "empty",
        block_seconds=1,
        file_seconds=2,
        root=tmp_path,
    )

    await cache.close()

    assert not cache.directory.exists()


@pytest.mark.asyncio
async def test_received_audio_cache_disables_itself_when_queue_is_full(
    tmp_path,
) -> None:
    cache = ReceivedAudioCache(
        "busy",
        block_seconds=1,
        file_seconds=2,
        root=tmp_path,
    )

    cache.append(bytes(PCM_BYTES_PER_SECOND * 5))
    assert cache.accepting is False

    await cache.close()

    assert len(list(cache.directory.glob("*.wav"))) == 2


@pytest.mark.asyncio
async def test_received_audio_cache_contains_background_write_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ReceivedAudioCache(
        "failure",
        block_seconds=1,
        file_seconds=2,
        root=tmp_path,
    )

    def fail(_block: bytes) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(cache._writer, "write", fail)
    cache.append(bytes(PCM_BYTES_PER_SECOND))

    await cache.close()

    assert cache.accepting is False


def test_received_audio_cache_rejects_invalid_periods(tmp_path) -> None:
    with pytest.raises(ValueError, match="整数倍"):
        ReceivedAudioCache(
            "invalid",
            block_seconds=3,
            file_seconds=5,
            root=tmp_path,
        )
