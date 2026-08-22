from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import grpc

from audio_harness.configs import EngineEndpoint
from audio_harness.errors import EngineUnavailableError
from audio_harness.inferencers.transports.grpc import (
    GrpcTransport,
    raise_grpc_error,
)
from audio_harness.inferencers.tts.base import TtsInferencer
from audio_harness.protos.tts import (
    preset_voice_pb2,
    preset_voice_pb2_grpc,
    tts_pb2,
    tts_pb2_grpc,
)


class QwenDuplexTtsInferencer(TtsInferencer):
    def __init__(
        self,
        endpoint: EngineEndpoint,
        *,
        max_message_bytes: int,
    ) -> None:
        self._transport = GrpcTransport(
            endpoint,
            max_message_bytes=max_message_bytes,
        )
        self.stub = tts_pb2_grpc.TtsServiceStub(self._transport.channel)
        self.preset_voice_stub = preset_voice_pb2_grpc.PresetVoiceServiceStub(
            self._transport.channel
        )

    async def ready(self) -> None:
        await self._transport.ready()

    async def probe(self) -> None:
        await self._transport.probe()

    async def close(self) -> None:
        await self._transport.close()

    async def list_preset_voices(self) -> TtsInferencer.PresetVoiceCatalog:
        try:
            response = await self.preset_voice_stub.ListPresetVoices(
                preset_voice_pb2.ListPresetVoicesRequest(),
                timeout=self._transport.endpoint.request_timeout_seconds,
            )
        except grpc.aio.AioRpcError as exc:
            raise_grpc_error(exc, "TTS 音色目录")
        return TtsInferencer.PresetVoiceCatalog(
            voices=tuple(
                TtsInferencer.PresetVoice(
                    voice_id=item.voice_id,
                    name=item.name,
                    language=item.language,
                    supports_synthesize=item.supports_synthesize,
                    supports_duplex=item.supports_duplex,
                    available_on_instance=item.available_on_instance,
                )
                for item in response.voices
            ),
            serving_mode=response.serving_mode,
        )

    @staticmethod
    def _voice_source(voice: TtsInferencer.Voice) -> tts_pb2.VoiceSource:
        if isinstance(voice, str):
            if not voice.strip():
                raise ValueError("预置音色 ID 不能为空")
            return tts_pb2.VoiceSource(
                preset_voice_id=voice.strip()
            )
        if isinstance(voice, TtsInferencer.ReferenceVoice):
            if voice.sample_rate <= 0:
                raise ValueError("参考音色采样率必须为正数")
            if not voice.pcm_s16le or len(voice.pcm_s16le) % 2:
                raise ValueError("参考音色必须是非空 PCM S16LE")
            return tts_pb2.VoiceSource(
                reference_voice=tts_pb2.ReferenceVoice(
                    pcm_s16le=voice.pcm_s16le,
                    sample_rate=voice.sample_rate,
                    ref_text=voice.ref_text,
                )
            )
        raise TypeError(f"未知音色类型: {type(voice)!r}")

    async def duplex_synthesize(
        self,
        text_chunks: AsyncIterator[str],
        *,
        voice: TtsInferencer.Voice,
        language: str,
        decoder_chunk_size: int,
    ) -> AsyncIterator[TtsInferencer.Chunk]:
        if decoder_chunk_size < 0 or decoder_chunk_size > 1024:
            raise ValueError("decoder_chunk_size 必须位于 0 到 1024")

        async def requests() -> AsyncIterator[tts_pb2.DuplexSynthesizeRequest]:
            yield tts_pb2.DuplexSynthesizeRequest(
                config=tts_pb2.DuplexStreamConfig(
                    voice=self._voice_source(voice),
                    language=language or "Auto",
                    decoder_chunk_size=decoder_chunk_size,
                )
            )
            async for text in text_chunks:
                if text:
                    yield tts_pb2.DuplexSynthesizeRequest(text_chunk=text)

        call = self.stub.DuplexSynthesize(
            requests(),
            # DuplexSynthesize 的生命周期等于一轮模型回复。普通请求超时
            # 会给整条双工流设置 deadline，长回复会在固定秒数后被截断。
            # 用户打断时由调用方取消此 RPC；连接阶段仍受 connect timeout
            # 保护。
        )
        expected_index = 0
        sample_rate: int | None = None
        completed = False
        try:
            async for chunk in call:
                if not chunk.pcm_s16le or len(chunk.pcm_s16le) % 2:
                    raise EngineUnavailableError(
                        "TTS 返回了空音频或非完整 PCM S16LE 样本"
                    )
                if chunk.sample_rate <= 0:
                    raise EngineUnavailableError("TTS 返回了非法采样率")
                if sample_rate is None:
                    sample_rate = chunk.sample_rate
                elif chunk.sample_rate != sample_rate:
                    raise EngineUnavailableError(
                        "TTS 在同一响应中改变了采样率"
                    )
                if chunk.chunk_index != expected_index:
                    raise EngineUnavailableError(
                        "TTS 音频块序号不连续: "
                        f"期望 {expected_index}，实际 {chunk.chunk_index}"
                    )
                expected_index += 1
                yield TtsInferencer.Chunk(
                    pcm_s16le=chunk.pcm_s16le,
                    sample_rate=chunk.sample_rate,
                    chunk_index=chunk.chunk_index,
                )
            completed = True
            if expected_index == 0:
                raise EngineUnavailableError("TTS 没有返回音频")
        except asyncio.CancelledError:
            call.cancel()
            raise
        except grpc.aio.AioRpcError as exc:
            raise_grpc_error(exc, "TTS")
        finally:
            if not completed:
                call.cancel()
