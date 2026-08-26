from __future__ import annotations

import asyncio
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
from audio_harness.inferencers.denoise import DenoiseInferencer
from audio_harness.inferencers.vad.base import VadInferencer
from audio_harness.media.pcm import PcmChunk, mix_pcm_s16le
from audio_harness.workflows import (
    AsrNode,
    AudioEdge,
    AudioGraphExecutionError,
    AudioGraphValidationError,
    AudioInferenceRequest,
    AudioRuntimeConfig,
    AudioWorkflowDocument,
    AudioWorkflowRuntime,
    FailurePolicy,
    GraphInputRef,
    NamedOutputBinding,
    NodeInputRef,
    NodeOutputRef,
    PhysicalVadConfig,
    PhysicalVadNode,
    StreamingDenoiseConfig,
    StreamingDenoiseNode,
    compile_audio_graph,
)


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


def _document(
    *,
    weight: float | None = None,
    failure_policy: FailurePolicy = FailurePolicy.FAIL_OPEN,
) -> AudioWorkflowDocument:
    return AudioWorkflowDocument(
        nodes=[
            StreamingDenoiseNode(
                id="denoise",
                engine_id="denoise-engine",
                config=StreamingDenoiseConfig(denoised_weight=weight),
                failure_policy=failure_policy,
            ),
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
        ],
        edges=[
            AudioEdge(
                id="input-denoise",
                source=GraphInputRef(),
                target=NodeInputRef(node_id="denoise", port="audio"),
            ),
            AudioEdge(
                id="denoise-vad",
                source=NodeOutputRef(node_id="denoise", port="audio"),
                target=NodeInputRef(node_id="vad", port="audio"),
            ),
            AudioEdge(
                id="vad-asr",
                source=NodeOutputRef(node_id="vad", port="segments"),
                target=NodeInputRef(node_id="asr", port="segments"),
            ),
        ],
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


class _Denoise(DenoiseInferencer):
    def __init__(self, denoised: bytes, *, fail_after: int | None = None) -> None:
        self.denoised = denoised
        self.fail_after = fail_after

    async def stream_synchronized(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
    ) -> AsyncIterator[DenoiseInferencer.SynchronizedChunk]:
        assert sample_rate == 4
        original = b"".join([chunk async for chunk in audio_chunks])
        split = self.fail_after or len(original)
        if split:
            yield DenoiseInferencer.SynchronizedChunk(
                original[:split], self.denoised[:split]
            )
        if self.fail_after is not None:
            raise RuntimeError("denoise failed")

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass


class _Vad(VadInferencer):
    def __init__(self) -> None:
        self.audio = bytearray()

    async def stream_detect(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int,
        *,
        postprocessing: VadInferencer.Postprocessing | None = None,
    ) -> AsyncIterator[VadInferencer.Update]:
        async for chunk in audio_chunks:
            self.audio.extend(chunk)
        samples = len(self.audio) // 2
        yield VadInferencer.Update(((0, samples),), samples)

    async def detect_turn(self, audio: bytes, sample_rate: int) -> bool:
        return True

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
            "ok",
            True,
            start_sample,
            end_sample,
            options.sample_rate,
            turn_completed,
        )

    async def ready(self) -> None: pass
    async def probe(self) -> None: pass
    async def close(self) -> None: pass


async def _chunks(original: bytes) -> AsyncIterator[PcmChunk]:
    yield PcmChunk(original[:4], 4, 0)
    yield PcmChunk(original[4:], 4, 2)


async def _run(
    definition: AudioWorkflowDocument,
    assets: dict[str, EngineAssetData],
    denoise: DenoiseInferencer,
) -> tuple[_Vad, _Asr]:
    original = np.array([1000, -1000, 30000, -30000], dtype="<i2").tobytes()
    vad = _Vad()
    asr = _Asr()
    runtime = AudioWorkflowRuntime(
        plan=compile_audio_graph(definition, assets=assets),
        inferencers={"denoise": denoise, "vad": vad, "asr": asr},
        runtime_config=AudioRuntimeConfig(2, 8),
        asr_semaphore=asyncio.Semaphore(1),
    )
    assert [
        output.name
        async for output in runtime.stream(
            _chunks(original),
            AudioInferenceRequest(
                recognition=AsrInferencer.Options(4, "zh-CN")
            ),
        )
    ] == ["primary_transcript"]
    return vad, asr


@pytest.mark.asyncio
async def test_denoise_weight_only_changes_vad_audio(
    assets: dict[str, EngineAssetData],
) -> None:
    original = np.array([1000, -1000, 30000, -30000], dtype="<i2").tobytes()
    denoised = np.array([-1000, 1000, 32000, -32000], dtype="<i2").tobytes()
    vad, asr = await _run(_document(weight=0.5), assets, _Denoise(denoised))

    assert bytes(vad.audio) == mix_pcm_s16le(original, denoised, 0.5)
    assert asr.audio == original


@pytest.mark.asyncio
async def test_denoise_fail_open_bypasses_unconfirmed_original(
    assets: dict[str, EngineAssetData],
) -> None:
    original = np.array([1000, -1000, 30000, -30000], dtype="<i2").tobytes()
    denoised = np.array([-1000, 1000, 32000, -32000], dtype="<i2").tobytes()
    vad, asr = await _run(
        _document(),
        assets,
        _Denoise(denoised, fail_after=4),
    )

    assert bytes(vad.audio) == denoised[:4] + original[4:]
    assert asr.audio == original


@pytest.mark.asyncio
async def test_denoise_fail_pipeline_stops_workflow(
    assets: dict[str, EngineAssetData],
) -> None:
    denoised = np.array([-1000, 1000, 32000, -32000], dtype="<i2").tobytes()

    with pytest.raises(AudioGraphExecutionError) as exc_info:
        await _run(
            _document(failure_policy=FailurePolicy.FAIL_PIPELINE),
            assets,
            _Denoise(denoised, fail_after=4),
        )

    assert exc_info.value.node_id == "denoise"


def test_private_denoise_audio_cannot_be_named_output(
    assets: dict[str, EngineAssetData],
) -> None:
    definition = _document()
    definition.outputs.append(
        NamedOutputBinding(
            name="debug_audio",
            source=NodeOutputRef(node_id="denoise", port="audio"),
        )
    )

    with pytest.raises(AudioGraphValidationError) as exc_info:
        compile_audio_graph(definition, assets=assets)

    assert "private_audio_output" in {
        diagnostic.code for diagnostic in exc_info.value.diagnostics
    }


def test_denoise_audio_cannot_feed_another_denoise(
    assets: dict[str, EngineAssetData],
) -> None:
    definition = _document()
    definition.nodes.append(
        StreamingDenoiseNode(id="other-denoise", engine_id="denoise-engine")
    )
    definition.edges.append(
        AudioEdge(
            id="private-to-other",
            source=NodeOutputRef(node_id="denoise", port="audio"),
            target=NodeInputRef(node_id="other-denoise", port="audio"),
        )
    )

    with pytest.raises(AudioGraphValidationError) as exc_info:
        compile_audio_graph(definition, assets=assets)

    assert "port_type_mismatch" in {
        diagnostic.code for diagnostic in exc_info.value.diagnostics
    }


@pytest.mark.parametrize("weight", [0.0, 0.5, 1.0])
def test_mix_pcm_s16le_weight_endpoints_and_midpoint(weight: float) -> None:
    original = np.array([-32768, 32767], dtype="<i2").tobytes()
    denoised = np.array([32767, -32768], dtype="<i2").tobytes()
    result = np.frombuffer(mix_pcm_s16le(original, denoised, weight), dtype="<i2")
    expected = {
        0.0: [-32768, 32767],
        0.5: [0, 0],
        1.0: [32767, -32768],
    }[weight]
    assert result.tolist() == expected
