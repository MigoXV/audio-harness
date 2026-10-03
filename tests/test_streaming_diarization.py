from __future__ import annotations

import asyncio
from contextlib import aclosing

import pytest

from audio_harness.inferencers.diarization.streaming_base import StreamingDiarizationInferencer as Engine
from audio_harness.media.pcm import PcmChunk
from audio_harness.sessions.contracts import SegmentCandidate, TimelineSpan
from audio_harness.sessions.diarization.streaming import StreamingDiarizationSession, StreamingDiarizationTimeline
from audio_harness.workflows.events import DiarizationClusterResult, DiarizationLocalResult
from audio_harness.workflows.models import StreamingSpeakerDiarizationConfig


async def iterate(values):
    for value in values:
        yield value


def candidate(start, end):
    return SegmentCandidate(bytes((1, 0)) * (end - start), start, end)


class FakeEngine(Engine):
    def __init__(self, *, fail=False, truncate=False, silence=False):
        self.fail = fail
        self.truncate = truncate
        self.silence = silence
        self.closed = False
        self.calls = 0

    async def ready(self):
        pass

    async def probe(self):
        pass

    async def close(self):
        pass

    async def stream_diarize(self, audio, sample_rate, *, input_buffer_latency_ms):
        self.calls += 1
        total = 0
        try:
            async for chunk in audio:
                first = total == 0
                total += len(chunk) // 2
                if self.fail and not first:
                    raise RuntimeError("engine unavailable")
                if not self.truncate:
                    yield self.Update(() if self.silence else (
                        self.Boundary(1, 2, 0 if first else None, None),
                    ), total)
            if total and not self.truncate:
                yield self.Update(() if self.silence else (self.Boundary(1, 2, None, total),), total)
        finally:
            self.closed = True


def session(engine=None, **config):
    return StreamingDiarizationSession(
        StreamingSpeakerDiarizationConfig(**config), 16000, engine or FakeEngine(), queue_capacity=1,
    )


@pytest.mark.asyncio
async def test_emits_turn_while_model_segment_and_input_remain_open():
    release = asyncio.Event()

    async def audio():
        yield PcmChunk(bytes(32000), 16000, 0)
        await release.wait()
        yield PcmChunk(bytes(32000), 16000, 16000)

    values = []
    async with aclosing(session().run(audio(), iterate([candidate(0, 16000), candidate(16000, 32000)]))) as stream:
        while True:
            port, value = await asyncio.wait_for(anext(stream), 2)
            values.append((port, value))
            if port == "cluster_results":
                break
        assert value.revision == 1
        assert value.assignments[0].speaker_id == 1
        release.set()
        values.extend([item async for item in stream])
    clusters = [value for port, value in values if port == "cluster_results"]
    assert [value.revision for value in clusters] == [1, 2]
    assert clusters[-1].replaces_revision == 1
    assert len(clusters[-1].assignments) == 2
    assert {value.speaker_id for value in clusters[-1].assignments} == {1}
    assert len({value.local_speaker_id for value in clusters[-1].assignments}) == 2
    segments = [value for port, value in values if port == "segments"]
    assert [value.diarization.segment_id for value in segments] == [value.segment_id for value in clusters[-1].assignments]
    assert [(value.start_sample, value.end_sample) for value in segments] == [(0, 16000), (16000, 32000)]


@pytest.mark.asyncio
async def test_failure_preserves_success_and_drains_remaining_audio():
    release = asyncio.Event()
    drained = False

    async def audio():
        nonlocal drained
        yield PcmChunk(bytes(32000), 16000, 0)
        await release.wait()
        yield PcmChunk(bytes(32000), 16000, 16000)
        yield PcmChunk(bytes(32000), 16000, 32000)
        drained = True

    engine = FakeEngine(fail=True)
    results = []
    async with aclosing(session(engine).run(audio(), iterate([candidate(0, 16000), candidate(16000, 48000)]))) as stream:
        while True:
            item = await asyncio.wait_for(anext(stream), 2)
            results.append(item)
            if item[0] == "cluster_results":
                break
        release.set()
        results.extend([item async for item in stream])
    clusters = [value for port, value in results if port == "cluster_results"]
    assert [value.phase for value in clusters] == ["completed", "failed"]
    assert clusters[-1].revision == 2 and clusters[-1].replaces_revision == 1
    assert clusters[-1].assignments == ()
    segments = [value for port, value in results if port == "segments"]
    assert segments[-1].diarization.speaker is None
    assert len(segments[-1].audio) == 64000
    assert drained and engine.closed and engine.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [{"max_pending_audio_seconds": 0.01}, {"max_segments": 1}])
async def test_resource_limits_fail_open(config):
    values = [item async for item in session(**config).run(
        iterate([PcmChunk(bytes(64000), 16000, 0)]),
        iterate([candidate(0, 16000), candidate(16000, 32000)]),
    )]
    assert any(isinstance(value, DiarizationClusterResult) and value.phase == "failed" for _, value in values)
    assert sum(len(value.audio) for port, value in values if port == "segments") == 64000


@pytest.mark.asyncio
async def test_missing_tail_watermark_fails_even_with_no_turns():
    values = [item async for item in session(FakeEngine(truncate=True)).run(
        iterate([PcmChunk(bytes(32000), 16000, 0)]), iterate([]),
    )]
    assert len(values) == 1
    assert values[0][1].phase == "failed"


@pytest.mark.asyncio
async def test_silence_unknown_speaker_and_empty_input():
    assert [item async for item in session().run(iterate([]), iterate([]))] == []
    values = [item async for item in session(FakeEngine(silence=True)).run(
        iterate([PcmChunk(bytes(32000), 16000, 0)]), iterate([candidate(0, 16000)]),
    )]
    assert not any(port == "cluster_results" for port, _ in values)
    segments = [value for port, value in values if port == "segments"]
    assert len(segments) == 1 and segments[0].diarization.speaker is None
    assert segments[0].diarization.min_asr_segment_seconds == 0
    assert segments[0].diarization.source_completed


@pytest.mark.asyncio
async def test_session_close_reaps_tasks():
    async def audio():
        yield PcmChunk(bytes(32000), 16000, 0)
        await asyncio.Event().wait()

    engine = FakeEngine()
    async with aclosing(session(engine).run(audio(), iterate([candidate(0, 16000)]))) as stream:
        await asyncio.wait_for(anext(stream), 2)
    assert engine.closed
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("diarization-duplex-")]


@pytest.mark.asyncio
async def test_rpc_failure_while_writer_waits_does_not_close_graph_audio():
    from tests.test_streaming_diarization_grpc import Service, running

    release = asyncio.Event()
    drained = False

    async def audio():
        nonlocal drained
        yield PcmChunk(bytes(320), 16000, 0)
        await release.wait()
        for index in range(1, 20):
            yield PcmChunk(bytes(320), 16000, index * 160)
        drained = True

    async with running(Service(malformed=True)) as engine:
        async with aclosing(session(engine).run(audio(), iterate([candidate(0, 3200)]))) as stream:
            port, failed = await asyncio.wait_for(anext(stream), 2)
            assert port == "cluster_results" and failed.phase == "failed"
            release.set()

            async def finish():
                return [item async for item in stream]

            rest = await asyncio.wait_for(finish(), 2)
    assert drained
    assert sum(len(value.audio) for port, value in rest if port == "segments") == 6400


def test_timeline_merges_boundaries_without_start_order_requirement():
    timeline = StreamingDiarizationTimeline(history_samples=16000, max_segments=10)
    timeline.accept(Engine.Update((Engine.Boundary(1, 1, 0, None),), 100), 1000)
    timeline.accept(Engine.Update((Engine.Boundary(1, 1, None, None), Engine.Boundary(2, 2, 110, 150)), 200), 1000)
    timeline.accept(Engine.Update((Engine.Boundary(1, 1, None, 300),), 400), 1000)
    assert timeline.evidence(candidate(0, 400)) == ((0, 300, 1), (110, 150, 2))
    timeline.accept(Engine.Update((), 1000), 1000)
    timeline.finish(1000)


@pytest.mark.parametrize("update", [
    Engine.Update((), 99),
    Engine.Update((), 1001),
    Engine.Update((Engine.Boundary(1, 2, None, None),), 200),
    Engine.Update((Engine.Boundary(2, 2, None, 150),), 200),
    Engine.Update((Engine.Boundary(2, 2, 50, 150),), 200),
    Engine.Update((Engine.Boundary(1, 1, None, 50),), 200),
    Engine.Update((Engine.Boundary(2, 9, 110, 150),), 200),
])
def test_timeline_rejects_mutation_and_invalid_progress(update):
    timeline = StreamingDiarizationTimeline(history_samples=16000, max_segments=10)
    timeline.accept(Engine.Update((Engine.Boundary(1, 1, 0, None),), 100), 1000)
    with pytest.raises(ValueError):
        timeline.accept(update, 1000)


@pytest.mark.asyncio
async def test_overlap_and_compressed_timeline_cover_audio_once():
    class Overlap(FakeEngine):
        async def stream_diarize(self, audio, sample_rate, *, input_buffer_latency_ms):
            async for _ in audio:
                pass
            yield self.Update((self.Boundary(1, 1, 0, 16000), self.Boundary(2, 2, 24000, 48000)), 48000)

    source = candidate(0, 32000)._replace(
        end_sample=48000,
        timeline_spans=(TimelineSpan(0, 16000, 0), TimelineSpan(16000, 32000, 32000)),
    )
    values = [item async for item in session(Overlap()).run(
        iterate([PcmChunk(bytes(96000), 16000, 0)]), iterate([source]),
    )]
    pieces = [value for port, value in values if port == "segments"]
    assert b"".join(value.audio for value in pieces) == source.audio
    assert [(value.start_sample, value.end_sample) for value in pieces] == [(0, 16000), (32000, 48000)]
    assert len({value.diarization.item_source_id for value in pieces}) == 1
    assert [value.diarization.source_completed for value in pieces] == [False, True]
    completed = next(value for _, value in values if isinstance(value, DiarizationLocalResult) and value.phase == "completed")
    assert completed.speech_samples == 32000
    assert completed.batch_end_sample == 48000


@pytest.mark.asyncio
async def test_same_start_overlap_uses_segment_id_order():
    class Overlap(FakeEngine):
        async def stream_diarize(self, audio, sample_rate, *, input_buffer_latency_ms):
            async for _ in audio:
                pass
            yield self.Update((self.Boundary(9, 2, 0, 12000), self.Boundary(1, 1, 0, 16000)), 16000)

    values = [item async for item in session(Overlap()).run(
        iterate([PcmChunk(bytes(32000), 16000, 0)]), iterate([candidate(0, 16000)]),
    )]
    pieces = [value for port, value in values if port == "segments"]
    assert len(pieces) == 1 and pieces[0].diarization.speaker == 2
    assert len(pieces[0].audio) == 32000
