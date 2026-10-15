from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import numpy as np
import pytest

from audio_harness.engines.models import (
    EngineApi,
    EngineAssetData,
    EngineKind,
    EngineProtocol,
    EngineStatus,
)
from audio_harness.inferencers.asr.base import AsrInferencer
from audio_harness.inferencers.denoise.base import DenoiseInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.media.agc import RealtimeAutomaticGainControl
from audio_harness.media.pcm import PcmChunk
from audio_harness.workflows.models import (
    AsrNode,
    AudioEdge,
    AudioWorkflowDocument,
    AutomaticGainControlConfig,
    AutomaticGainControlNode,
    GraphInputRef,
    NamedOutputBinding,
    NodeInputRef,
    NodeOutputRef,
    PhysicalVadConfig,
    PhysicalVadNode,
    StreamingDenoiseNode,
)
from audio_harness.workflows.compiler import (
    AudioGraphValidationError,
    compile_audio_graph,
)
from audio_harness.workflows.runtime import (
    AudioInferenceRequest,
    AudioRuntimeConfig,
    AudioWorkflowRuntime,
)


def _processor(**overrides: float) -> RealtimeAutomaticGainControl:
    values = {
        "sample_rate": 16000,
        "attack_seconds": 0.0,
        "decay_seconds": 2.5,
        "soft_knee_db": 6.0,
        "initial_level_db": -12.0,
        "output_gain_db": 4.5,
    }
    values.update(overrides)
    return RealtimeAutomaticGainControl(**values)


def test_agc_matches_ffmpeg_reference_hash() -> None:
    pattern = np.array(
        [
            0, 1, -1, 32, -32, 128, -128, 512, -512, 2048,
            -2048, 8192, -8192, 16000, -16000, 30000, -30000,
            1000, -1000, 0,
        ],
        dtype="<i2",
    )
    pcm = np.tile(pattern, 2000).tobytes()
    processor = _processor()
    output = b"".join(
        processor.process(pcm[index : index + 1278])
        for index in range(0, len(pcm), 1278)
    )
    processor.close()

    assert hashlib.sha256(output).hexdigest() == (
        "f8a212f8f7fd96f386fd36616e1f8bcebc30675d71dfbeb43f5c0fca4bfc7a70"
    )


def test_agc_is_chunk_boundary_independent() -> None:
    samples = np.array(
        [0, 32, -120, 700, -4000, 16000, -32768, 8000, 0, -200],
        dtype="<i2",
    )
    whole = _processor().process(samples.tobytes())
    split_processor = _processor()
    split = b"".join(
        split_processor.process(part.tobytes())
        for part in np.array_split(samples, [3, 7])
    )

    assert split == whole


def test_agc_saturates_output() -> None:
    sample = 1036
    processor = _processor(
        soft_knee_db=0.01,
        output_gain_db=24,
        initial_level_db=-30,
    )
    loud = np.array([sample], dtype="<i2").tobytes()
    assert np.frombuffer(processor.process(loud), dtype="<i2")[0] == 32767


def test_agc_zero_attack_follows_a_louder_sample_immediately() -> None:
    sample = 16000
    pcm = np.array([sample], dtype="<i2").tobytes()
    quiet_start = _processor(initial_level_db=-80).process(pcm)
    loud_start = _processor(initial_level_db=-12).process(pcm)

    assert quiet_start == loud_start


def test_agc_decay_controls_gain_recovery() -> None:
    loud_then_quiet = np.array([30000, *([1000] * 1600)], dtype="<i2").tobytes()

    slow = _processor(decay_seconds=2.5).process(loud_then_quiet)
    fast = _processor(decay_seconds=0.01).process(loud_then_quiet)

    assert slow != fast


def test_agc_soft_knee_smooths_a_curve_corner() -> None:
    sample = 65
    pcm = np.array([sample], dtype="<i2").tobytes()
    linear = _processor(
        soft_knee_db=0.01,
        initial_level_db=-54,
    ).process(pcm)
    softened = _processor(
        soft_knee_db=6,
        initial_level_db=-54,
    ).process(pcm)

    assert softened != linear


def test_agc_rejects_partial_pcm_sample() -> None:
    with pytest.raises(ValueError, match="PCM S16LE"):
        _processor().process(b"\x00")


def _asset(asset_id: str, kind: EngineKind, api: EngineApi) -> EngineAssetData:
    now = datetime.now(timezone.utc)
    return EngineAssetData(
        id=asset_id,
        slug=asset_id,
        name=asset_id,
        kind=kind,
        api=api,
        protocol=EngineProtocol.GRPC,
        target="test:1",
        connect_timeout_seconds=1,
        request_timeout_seconds=None,
        enabled=True,
        status=EngineStatus.CONFIRMED_RUNNING,
        image=None,
        description=None,
        status_basis=None,
        status_observed_at=None,
        source=None,
        details={},
        revision=1,
        created_at=now,
        updated_at=now,
    )


class _Vad(VadInferencer):
    def __init__(self) -> None:
        self.audio = b""

    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
        *,
        postprocessing: VadInferencer.Postprocessing | None = None,
    ) -> AsyncIterator[VadInferencer.Update]:
        self.audio = b"".join([chunk async for chunk in audio_chunks])
        samples = len(self.audio) // 2
        yield VadInferencer.Update(((0, samples),), samples)

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        return True

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass


class _Denoise(DenoiseInferencer):
    async def stream_synchronized(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[DenoiseInferencer.SynchronizedChunk]:
        async for chunk in audio_chunks:
            yield DenoiseInferencer.SynchronizedChunk(
                original_pcm_s16le=chunk,
                denoised_pcm_s16le=b"\x00" * len(chunk),
            )

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass


class _Asr(AsrInferencer):
    def __init__(self) -> None:
        self.audio = b""

    async def transcribe(
        self,
        audio: bytes,
        *,
        start_sample: int,
        end_sample: int,
        turn_completed: bool,
        options: AsrInferencer.Options,
    ) -> AsyncIterator[AsrInferencer.Result]:
        self.audio = audio
        yield AsrInferencer.Result(
            "ok", True, start_sample, end_sample, options.sample_rate, turn_completed
        )

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass


def _document(*, both_vad_inputs: bool = False) -> AudioWorkflowDocument:
    edges = [
        AudioEdge(
            id="input-agc",
            source=GraphInputRef(),
            target=NodeInputRef(node_id="agc", port="audio"),
        ),
        AudioEdge(
            id="agc-vad",
            source=NodeOutputRef(node_id="agc", port="audio"),
            target=NodeInputRef(node_id="vad", port="audio"),
        ),
        AudioEdge(
            id="vad-asr",
            source=NodeOutputRef(node_id="vad", port="segments"),
            target=NodeInputRef(node_id="asr", port="segments"),
        ),
    ]
    if both_vad_inputs:
        edges.extend([
            AudioEdge(
                id="input-denoise",
                source=GraphInputRef(),
                target=NodeInputRef(node_id="denoise", port="audio"),
            ),
            AudioEdge(
                id="denoise-vad-detection",
                source=NodeOutputRef(node_id="denoise", port="audio"),
                target=NodeInputRef(node_id="vad", port="detection_audio"),
            ),
        ])
    nodes = [
        AutomaticGainControlNode(id="agc"),
        PhysicalVadNode(
            id="vad",
            engine_id="vad-engine",
            config=PhysicalVadConfig(
                min_segment_seconds=0,
                max_segment_seconds=60,
                audio_buffer_seconds=61,
                speech_pre_roll_seconds=0,
            ),
        ),
        AsrNode(id="asr", engine_id="asr-engine"),
    ]
    if both_vad_inputs:
        nodes.append(
            StreamingDenoiseNode(id="denoise", engine_id="denoise-engine")
        )
    return AudioWorkflowDocument(
        nodes=nodes,
        edges=edges,
        outputs=[
            NamedOutputBinding(
                name="primary_transcript",
                source=NodeOutputRef(node_id="asr", port="transcripts"),
            ),
            NamedOutputBinding(
                name="speech_started",
                source=NodeOutputRef(node_id="vad", port="speech_started"),
            ),
            NamedOutputBinding(
                name="speech_stopped",
                source=NodeOutputRef(node_id="vad", port="speech_stopped"),
            ),
        ],
    )


@pytest.fixture
def assets() -> dict[str, EngineAssetData]:
    return {
        "denoise-engine": _asset(
            "denoise-engine", EngineKind.DENOISE, EngineApi.UX_DENOISE
        ),
        "vad-engine": _asset("vad-engine", EngineKind.VAD, EngineApi.UX_VAD),
        "asr-engine": _asset("asr-engine", EngineKind.ASR, EngineApi.UX_SPEECH),
    }


@pytest.mark.asyncio
async def test_agc_audio_is_used_by_vad_and_asr(
    assets: dict[str, EngineAssetData],
) -> None:
    original = np.array([100, -1000, 12000, -30000], dtype="<i2").tobytes()
    expected = _processor().process(original)
    vad = _Vad()
    asr = _Asr()
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(_document(), assets=assets),
        inferencers={"vad": vad, "asr": asr},
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=asyncio.Semaphore(1),
    )

    async def chunks() -> AsyncIterator[PcmChunk]:
        yield PcmChunk(original[:4], 16000, 0)
        yield PcmChunk(original[4:], 16000, 2)

    outputs = [
        output.name
        async for output in runtime.stream(
            chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(16000, "zh-CN")
            ),
        )
    ]

    assert outputs == ["primary_transcript"]
    assert vad.audio == expected
    assert asr.audio == expected


@pytest.mark.asyncio
async def test_denoise_after_agc_only_changes_vad_detection_audio(
    assets: dict[str, EngineAssetData],
) -> None:
    definition = _document()
    definition.nodes.append(
        StreamingDenoiseNode(id="denoise", engine_id="denoise-engine")
    )
    definition.edges = [
        edge for edge in definition.edges if edge.id != "agc-vad"
    ]
    definition.edges.extend(
        [
            AudioEdge(
                id="agc-denoise",
                source=NodeOutputRef(node_id="agc", port="audio"),
                target=NodeInputRef(node_id="denoise", port="audio"),
            ),
            AudioEdge(
                id="denoise-vad",
                source=NodeOutputRef(node_id="denoise", port="audio"),
                target=NodeInputRef(node_id="vad", port="detection_audio"),
            ),
        ]
    )
    original = np.array([100, -1000, 12000, -30000], dtype="<i2").tobytes()
    expected_main = _processor().process(original)
    vad = _Vad()
    asr = _Asr()
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(definition, assets=assets),
        inferencers={"denoise": _Denoise(), "vad": vad, "asr": asr},
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=asyncio.Semaphore(1),
    )

    async def chunks() -> AsyncIterator[PcmChunk]:
        yield PcmChunk(original, 16000, 0)

    assert [
        output.name
        async for output in runtime.stream(
            chunks(),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(16000, "zh-CN")
            ),
        )
    ] == ["primary_transcript"]
    assert vad.audio == b"\x00" * len(original)
    assert asr.audio == expected_main


def test_physical_vad_audio_inputs_are_mutually_exclusive(
    assets: dict[str, EngineAssetData],
) -> None:
    with pytest.raises(AudioGraphValidationError) as exc_info:
        compile_audio_graph(_document(both_vad_inputs=True), assets=assets)

    assert "mutually_exclusive_inputs" in {
        diagnostic.code for diagnostic in exc_info.value.diagnostics
    }


def test_physical_vad_requires_one_audio_input(
    assets: dict[str, EngineAssetData],
) -> None:
    definition = _document()
    definition.edges = [
        edge for edge in definition.edges if edge.id != "agc-vad"
    ]

    with pytest.raises(AudioGraphValidationError) as exc_info:
        compile_audio_graph(definition, assets=assets)

    assert "required_input_group_missing" in {
        diagnostic.code for diagnostic in exc_info.value.diagnostics
    }


def test_agc_pcm_cannot_be_named_output(
    assets: dict[str, EngineAssetData],
) -> None:
    definition = _document()
    definition.outputs.append(
        NamedOutputBinding(
            name="debug_audio",
            source=NodeOutputRef(node_id="agc", port="audio"),
        )
    )

    with pytest.raises(AudioGraphValidationError) as exc_info:
        compile_audio_graph(definition, assets=assets)

    assert "private_audio_output" in {
        diagnostic.code for diagnostic in exc_info.value.diagnostics
    }
