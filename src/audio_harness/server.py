from __future__ import annotations

import logging

import grpc

from audio_harness.config import AppConfig
from audio_harness.domain import EngineUnavailableError
from audio_harness.engines.grpc import GrpcAsrEngine, GrpcVadEngine
from audio_harness.protos.asr import ux_speech_pb2_grpc
from audio_harness.servicers.speech import SpeechServicer

logger = logging.getLogger(__name__)


class AudioHarnessServer:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        max_message_bytes = config.serving.max_message_bytes
        self.asr = GrpcAsrEngine(
            config.engines.asr,
            min_chunk_bytes=config.workflow.min_chunk_bytes,
            max_message_bytes=max_message_bytes,
        )
        self.vad = GrpcVadEngine(
            config.engines.vad,
            max_message_bytes=max_message_bytes,
        )
        self.semantic_vad = (
            GrpcVadEngine(
                config.engines.semantic_vad,
                max_message_bytes=max_message_bytes,
            )
            if config.engines.semantic_vad is not None
            else None
        )
        options = (
            ("grpc.max_send_message_length", max_message_bytes),
            ("grpc.max_receive_message_length", max_message_bytes),
        )
        self.grpc_server = grpc.aio.server(options=options)
        self.bound_port: int | None = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        try:
            await self.asr.ready()
            await self.vad.ready()
            if self.semantic_vad is not None:
                try:
                    await self.semantic_vad.ready()
                except EngineUnavailableError:
                    logger.warning(
                        "语义 VAD 启动检查失败，运行时将按失败放行策略处理",
                        exc_info=True,
                    )

            servicer = SpeechServicer(
                config=self.config,
                asr=self.asr,
                vad=self.vad,
                semantic_vad=self.semantic_vad,
            )
            ux_speech_pb2_grpc.add_UxSpeechServicer_to_server(
                servicer,
                self.grpc_server,
            )
            address = f"{self.config.serving.host}:{self.config.serving.port}"
            self.bound_port = self.grpc_server.add_insecure_port(address)
            if self.bound_port == 0:
                raise RuntimeError(f"无法监听 gRPC 地址: {address}")
            await self.grpc_server.start()
            self._started = True
            logger.info(
                "Audio Harness 已启动: address=%s, asr=%s, vad=%s, semantic_vad=%s",
                address,
                self.config.engines.asr.target,
                self.config.engines.vad.target,
                (
                    self.config.engines.semantic_vad.target
                    if self.config.engines.semantic_vad is not None
                    else None
                ),
            )
        except Exception:
            await self._close_engines()
            self._closed = True
            raise

    async def wait_for_termination(self) -> None:
        await self.grpc_server.wait_for_termination()

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            await self.grpc_server.stop(self.config.serving.shutdown_grace_seconds)
        await self._close_engines()
        logger.info("Audio Harness 已停止")

    async def _close_engines(self) -> None:
        await self.asr.close()
        await self.vad.close()
        if self.semantic_vad is not None:
            await self.semantic_vad.close()
