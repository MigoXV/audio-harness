from __future__ import annotations

import logging

from audio_harness.api.service import EngineCatalog
from audio_harness.capacity import ServingCapacity
from audio_harness.capacity_settings import CapacitySettingsStore
from audio_harness.config import AppConfig, CapacityConfig
from audio_harness.database import Database
from audio_harness.engine_health import EngineHealthMonitor
from audio_harness.engines import InferencerPool
from audio_harness.inferencers.factory import DefaultInferencerFactory
from audio_harness.serving.grpc import GrpcServer, SpeechServicer
from audio_harness.serving.http import create_http_app
from audio_harness.serving.realtime import RealtimeTranscriptionService
from audio_harness.serving.realtime.tools import ToolRegistry
from audio_harness.serving.status import ServingStatusService
from audio_harness.serving.webrtc import WebRtcCallService
from audio_harness.sessions import AudioWorkflowSessionService
from audio_harness.voice_agents import (
    RealtimeRouteService,
    VoiceAgentRepository,
    VoiceAgentService,
)
from audio_harness.voices import VoiceAssetStore
from audio_harness.workflow_settings import WorkflowSettingsStore
from audio_harness.workflows import AudioWorkflowRepository, AudioWorkflowService

logger = logging.getLogger(__name__)


class AudioHarnessHost:
    """进程级组合根；业务对象不在协议层自行查库或拼装。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        max_message_bytes = config.limits.max_message_bytes
        self.database = Database(config.database.url)

        self.inferencer_pool = InferencerPool(
            inferencer_factory=DefaultInferencerFactory(
                min_chunk_bytes=config.workflow.min_chunk_bytes,
                max_message_bytes=max_message_bytes,
            )
        )
        self.catalog = EngineCatalog(
            self.database.session_factory,
            self.inferencer_pool,
        )
        self.engine_health = EngineHealthMonitor(self.catalog)

        self.audio_workflows = AudioWorkflowService(
            AudioWorkflowRepository(self.database.session_factory),
            inferencer_pool=self.inferencer_pool,
        )
        self.voice_agents = VoiceAgentService(
            VoiceAgentRepository(self.database.session_factory),
            audio_workflows=self.audio_workflows,
            inferencer_pool=self.inferencer_pool,
        )
        self.realtime_routes = RealtimeRouteService(
            self.database.session_factory,
            audio_workflows=self.audio_workflows,
            voice_agents=self.voice_agents,
        )
        self.workflow_settings = WorkflowSettingsStore(
            self.database.session_factory,
            max_message_bytes=max_message_bytes,
        )
        self.serving_status = ServingStatusService(
            database=self.database,
            workflows=self.audio_workflows,
            workflow_settings=self.workflow_settings,
            routes=self.realtime_routes,
        )
        self.capacity_settings = CapacitySettingsStore(
            self.database.session_factory
        )
        self.capacity = ServingCapacity(CapacityConfig())
        self.capacity_settings.subscribe(self.capacity.apply)
        self.voice_store = VoiceAssetStore(
            self.database.session_factory,
            storage_dir=config.voices.storage_dir,
            max_upload_bytes=config.voices.max_upload_bytes,
        )
        self.tool_registry = ToolRegistry()

        self.grpc_sessions = self._session_service(self.capacity.grpc_sessions)
        self.realtime_sessions = self._session_service(
            self.capacity.realtime_sessions
        )
        self.webrtc_sessions = self._session_service(self.capacity.webrtc_sessions)

        self.realtime_service = RealtimeTranscriptionService(
            session_service=self.realtime_sessions,
            workflow_settings=self.workflow_settings,
            routes=self.realtime_routes,
            max_message_bytes=max_message_bytes,
            inferencer_pool=self.inferencer_pool,
            responses_limiter=self.capacity.responses,
            tts_limiter=self.capacity.tts,
            voice_resolver=self.voice_store.resolve,
            tool_registry=self.tool_registry,
        )
        self.webrtc_service = WebRtcCallService(
            config=config.webrtc,
            session_service=self.webrtc_sessions,
            workflow_settings=self.workflow_settings,
            routes=self.realtime_routes,
            max_message_bytes=max_message_bytes,
            inferencer_pool=self.inferencer_pool,
            responses_limiter=self.capacity.responses,
            tts_limiter=self.capacity.tts,
            voice_resolver=self.voice_store.resolve,
            tool_registry=self.tool_registry,
        )
        self.http_app = create_http_app(
            catalog=self.catalog,
            workflows=self.audio_workflows,
            voice_agents=self.voice_agents,
            routes=self.realtime_routes,
            serving_status=self.serving_status,
            workflow_settings=self.workflow_settings,
            capacity_settings=self.capacity_settings,
            capacity=self.capacity,
            realtime_service=self.realtime_service,
            webrtc_service=self.webrtc_service,
            max_message_bytes=max_message_bytes,
            voice_store=self.voice_store,
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

    def _session_service(self, session_limiter) -> AudioWorkflowSessionService:
        return AudioWorkflowSessionService(
            inferencer_pool=self.inferencer_pool,
            workflows=self.audio_workflows,
            workflow_settings=self.workflow_settings,
            session_limiter=session_limiter,
            asr_limiter=self.capacity.asr,
        )

    async def start(self) -> None:
        try:
            await self.database.ping()
            await self.workflow_settings.initialize()
            initial_capacity = await self.capacity_settings.initialize()
            await self.capacity.apply(initial_capacity)
            await self.audio_workflows.initialize()
            await self.voice_agents.initialize()
            await self.realtime_routes.initialize()
            await self.grpc_server.start()
            self.engine_health.start()
            self._started = True
        except Exception:
            await self.inferencer_pool.close()
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
        await self.engine_health.close()
        await self.webrtc_service.close()
        await self.inferencer_pool.close()
        await self.database.close()
        logger.info("Audio Harness 已停止")
