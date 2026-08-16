from __future__ import annotations

import logging

import grpc

from audio_harness.configs import GrpcConfig
from audio_harness.protos.asr import ux_speech_pb2_grpc
from audio_harness.serving.grpc.speech import SpeechServicer

logger = logging.getLogger(__name__)


class GrpcServer:
    def __init__(
        self,
        *,
        config: GrpcConfig,
        max_message_bytes: int,
        servicer: SpeechServicer,
    ) -> None:
        self.config = config
        self.servicer = servicer
        options = (
            ("grpc.max_send_message_length", max_message_bytes),
            ("grpc.max_receive_message_length", max_message_bytes),
        )
        self._server = grpc.aio.server(options=options)
        self.bound_port: int | None = None
        self._started = False

    async def start(self) -> None:
        ux_speech_pb2_grpc.add_UxSpeechServicer_to_server(
            self.servicer,
            self._server,
        )
        address = f"{self.config.host}:{self.config.port}"
        self.bound_port = self._server.add_insecure_port(address)
        if self.bound_port == 0:
            raise RuntimeError(f"无法监听 gRPC 地址: {address}")
        await self._server.start()
        self._started = True
        logger.info(
            "Audio Harness gRPC 已启动: address=%s:%s",
            self.config.host,
            self.bound_port,
        )

    async def wait_for_termination(self) -> None:
        await self._server.wait_for_termination()

    async def stop(self) -> None:
        if self._started:
            await self._server.stop(5.0)
            self._started = False
