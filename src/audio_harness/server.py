from __future__ import annotations

import logging

import grpc

from audio_harness.api import create_management_app
from audio_harness.api.service import EngineCatalog
from audio_harness.config import AppConfig
from audio_harness.database import Database
from audio_harness.engines.router import EngineRouter
from audio_harness.inferencers.grpc import GrpcInferencerFactory
from audio_harness.protos.asr import ux_speech_pb2_grpc
from audio_harness.servicers.speech import SpeechServicer

logger = logging.getLogger(__name__)


class AudioHarnessServer:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        max_message_bytes = config.serving.max_message_bytes
        self.database = Database(config.database.url)
        self.engine_router = EngineRouter(
            self.database.session_factory,
            inferencer_factory=GrpcInferencerFactory(
                min_chunk_bytes=config.workflow.min_chunk_bytes,
                max_message_bytes=max_message_bytes,
            ),
        )
        self.catalog = EngineCatalog(
            self.database.session_factory,
            self.engine_router,
        )
        self.management_app = create_management_app(
            database=self.database,
            catalog=self.catalog,
            engine_router=self.engine_router,
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
            await self.database.ping()
            await self.engine_router.prewarm()

            servicer = SpeechServicer(
                config=self.config,
                engine_provider=self.engine_router,
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
                "Audio Harness gRPC 数据面已启动: address=%s",
                address,
            )
        except Exception:
            await self.engine_router.close()
            await self.database.close()
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
        await self.engine_router.close()
        await self.database.close()
        logger.info("Audio Harness 已停止")
