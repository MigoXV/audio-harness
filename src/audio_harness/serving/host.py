from __future__ import annotations

import logging

from audio_harness.api.service import EngineCatalog
from audio_harness.capacity import ServingCapacity
from audio_harness.capacity_settings import CapacitySettingsStore
from audio_harness.config import AppConfig, CapacityConfig
from audio_harness.database import Database
from audio_harness.engines.router import EngineRouter
from audio_harness.inferencers.grpc import GrpcInferencerFactory
from audio_harness.pipelines.dag import default_node_registry
from audio_harness.pipelines.service import PipelineStore
from audio_harness.serving.grpc import GrpcServer, SpeechServicer
from audio_harness.serving.http import create_http_app
from audio_harness.serving.realtime import RealtimeTranscriptionService
from audio_harness.serving.webrtc import WebRtcCallService
from audio_harness.sessions import TranscriptionSessionService
from audio_harness.workflow_settings import WorkflowSettingsStore

logger = logging.getLogger(__name__)


class AudioHarnessHost:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        max_message_bytes = config.limits.max_message_bytes
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
        self.pipeline_store = PipelineStore(
            self.database.session_factory,
            engine_router=self.engine_router,
            registry=default_node_registry(),
        )
        self.workflow_settings = WorkflowSettingsStore(
            self.database.session_factory,
            max_message_bytes=max_message_bytes,
        )
        self.capacity_settings = CapacitySettingsStore(
            self.database.session_factory
        )
        self.capacity = ServingCapacity(CapacityConfig())
        self.capacity_settings.subscribe(self.capacity.apply)
        self.grpc_sessions = TranscriptionSessionService(
            engine_router=self.engine_router,
            pipeline_store=self.pipeline_store,
            workflow_settings=self.workflow_settings,
            session_limiter=self.capacity.grpc_sessions,
            asr_limiter=self.capacity.asr,
        )
        self.realtime_sessions = TranscriptionSessionService(
            engine_router=self.engine_router,
            pipeline_store=self.pipeline_store,
            workflow_settings=self.workflow_settings,
            session_limiter=self.capacity.realtime_sessions,
            asr_limiter=self.capacity.asr,
        )
        self.webrtc_sessions = TranscriptionSessionService(
            engine_router=self.engine_router,
            pipeline_store=self.pipeline_store,
            workflow_settings=self.workflow_settings,
            session_limiter=self.capacity.webrtc_sessions,
            asr_limiter=self.capacity.asr,
        )
        if config.realtime.api_key is None:
            raise ValueError("必须配置 AUDIO_HARNESS_REALTIME_API_KEY")
        self.realtime_service = RealtimeTranscriptionService(
            session_service=self.realtime_sessions,
            workflow_settings=self.workflow_settings,
            pipeline_store=self.pipeline_store,
            api_key=config.realtime.api_key.get_secret_value(),
            max_message_bytes=max_message_bytes,
        )
        self.webrtc_service = WebRtcCallService(
            config=config.webrtc,
            session_service=self.webrtc_sessions,
            workflow_settings=self.workflow_settings,
            pipeline_store=self.pipeline_store,
            api_key=config.realtime.api_key.get_secret_value(),
            max_message_bytes=max_message_bytes,
        )
        self.http_app = create_http_app(
            database=self.database,
            catalog=self.catalog,
            engine_router=self.engine_router,
            pipeline_store=self.pipeline_store,
            workflow_settings=self.workflow_settings,
            capacity_settings=self.capacity_settings,
            realtime_service=self.realtime_service,
            webrtc_service=self.webrtc_service,
            max_message_bytes=max_message_bytes,
        )
        self.grpc_server = GrpcServer(
            config=config.grpc,
            max_message_bytes=max_message_bytes,
            servicer=SpeechServicer(
                config=config,
                session_service=self.grpc_sessions,
                workflow_settings=self.workflow_settings,
            ),
        )
        self._started = False
        self._closed = False

    async def start(self) -> None:
        try:
            await self.database.ping()
            await self.workflow_settings.initialize()
            initial_capacity = await self.capacity_settings.initialize()
            await self.capacity.apply(initial_capacity)
            await self.pipeline_store.initialize()
            active = self.pipeline_store.current_or_none()
            if active is not None:
                await self.engine_router.prepare_all(
                    active.plan.engine_snapshots
                )
            await self.grpc_server.start()
            self._started = True
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
            await self.grpc_server.stop()
        await self.webrtc_service.close()
        await self.engine_router.close()
        await self.database.close()
        logger.info("Audio Harness 已停止")
