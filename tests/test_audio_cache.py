from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import wave

import pytest

from audio_harness.media import audio_cache
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

    assert cache.directory is not None
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

    assert cache.directory is None


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

    assert cache.directory is not None
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

    def fail(_block: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(cache._writer, "write", fail)
    cache.append(bytes(PCM_BYTES_PER_SECOND))

    await cache.close()

    assert cache.accepting is False


@pytest.mark.asyncio
async def test_received_audio_cache_uses_received_time_for_directory_and_wavs(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime(2026, 8, 20, 8, 13, 8, 665_000, tzinfo=timezone.utc)
    received_times = iter((started_at, started_at + timedelta(seconds=1)))
    monkeypatch.setattr(audio_cache, "_utc_now", lambda: next(received_times))
    session_id = "sess_a9044150e396453e930444cbe100ac41"
    cache = ReceivedAudioCache(
        session_id,
        block_seconds=1,
        file_seconds=1,
        root=tmp_path,
    )

    cache.append(bytes(PCM_BYTES_PER_SECOND))
    cache.append(bytes(PCM_BYTES_PER_SECOND))
    await cache.close()

    assert cache.directory == (
        tmp_path
        / "2026-08-20"
        / "20260820T081308.665Z__sess-a9044150"
    )
    paths = sorted(cache.directory.glob("*.wav"))
    assert [path.name for path in paths] == [
        "20260820T081308.665Z_000000.wav",
        "20260820T081309.665Z_000001.wav",
    ]
    metadata = json.loads((cache.directory / "metadata.json").read_text())
    assert metadata == {
        "schema_version": 1,
        "session_id": session_id,
        "started_at": "2026-08-20T08:13:08.665000Z",
        "audio_format": {
            "sample_rate_hz": 16_000,
            "channels": 1,
            "sample_format": "pcm_s16le",
        },
        "block_seconds": 1,
        "file_seconds": 1,
    }
    assert (cache.directory / "metadata.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_received_audio_cache_adds_sequence_for_directory_collisions(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime(2026, 8, 20, 8, 13, 8, 665_000, tzinfo=timezone.utc)
    monkeypatch.setattr(audio_cache, "_utc_now", lambda: started_at)

    caches = [
        ReceivedAudioCache(
            "sess_a9044150e396453e930444cbe100ac41",
            block_seconds=1,
            file_seconds=1,
            root=tmp_path,
        )
        for _ in range(2)
    ]
    for cache in caches:
        cache.append(bytes(PCM_BYTES_PER_SECOND))
        await cache.close()

    assert [cache.directory.name for cache in caches if cache.directory] == [
        "20260820T081308.665Z__sess-a9044150",
        "20260820T081308.665Z__sess-a9044150__02",
    ]


def test_received_audio_cache_rejects_invalid_periods(tmp_path) -> None:
    with pytest.raises(ValueError, match="整数倍"):
        ReceivedAudioCache(
            "invalid",
            block_seconds=3,
            file_seconds=5,
            root=tmp_path,
        )
