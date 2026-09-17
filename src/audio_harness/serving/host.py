from __future__ import annotations

from functools import partial
import logging

from audio_harness.agent_backends.catalog import AgentBackendCatalog
from audio_harness.agent_backends.dsh import DshWebGateway
from audio_harness.agent_backends.health import AgentBackendHealthMonitor
from audio_harness.api import create_http_app
from audio_harness.configs import AppConfig, CapacityConfig
from audio_harness.database import Database
from audio_harness.engines.health import EngineHealthMonitor
from audio_harness.engines.catalog import EngineCatalog
from audio_harness.engines.inferencer_factory import create_inferencer
from audio_harness.engines.pool import InferencerPool
from audio_harness.serving.aha_grpc.server import AhaGrpcServer
from audio_harness.serving.model_catalog import RealtimeModelCatalog
from audio_harness.serving.capacity import ServingCapacity
from audio_harness.serving.capacity_settings import CapacitySettingsStore
from audio_harness.serving.realtime import RealtimeTranscriptionService
from audio_harness.serving.realtime.tools import ToolRegistry
from audio_harness.serving.status import ServingStatusService
from audio_harness.serving.webrtc import WebRtcCallService
from audio_harness.sessions.audio_workflow import AudioWorkflowSessionService
from audio_harness.voice_agents import (
    VoiceAgentRepository,
    VoiceAgentService,
)
from audio_harness.voice_agents.voices import VoiceAssetStore
from audio_harness.workflows.settings import WorkflowSettingsStore
from audio_harness.workflows import AudioWorkflowRepository, AudioWorkflowService
from audio_harness.vectors import VectorService
from audio_harness.vectors.qdrant import QdrantGateway

logger = logging.getLogger(__name__)


class AudioHarnessHost:
    """进程级组合根；业务对象不在协议层自行查库或拼装。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        max_message_bytes = config.limits.max_message_bytes
        self.database = Database(config.database.url)

        self.inferencer_pool = InferencerPool(
            create_inferencer=partial(
                create_inferencer,
                max_message_bytes=max_message_bytes,
            )
        )
        self.catalog = EngineCatalog(
            self.database.session_factory,
            self.inferencer_pool,
        )
        self.engine_health = EngineHealthMonitor(self.catalog)
        self.agent_backend_gateway = DshWebGateway()
        self.agent_backends = AgentBackendCatalog(
            self.database.session_factory,
            self.agent_backend_gateway,
        )
        self.agent_backend_health = AgentBackendHealthMonitor(self.agent_backends)

        self.audio_workflows = AudioWorkflowService(
            AudioWorkflowRepository(self.database.session_factory),
            inferencer_pool=self.inferencer_pool,
        )
        self.voice_agents = VoiceAgentService(
            VoiceAgentRepository(self.database.session_factory),
            audio_workflows=self.audio_workflows,
            inferencer_pool=self.inferencer_pool,
            agent_backends=self.agent_backends,
        )
        self.realtime_models = RealtimeModelCatalog(
            audio_workflows=self.audio_workflows,
            voice_agents=self.voice_agents,
        )
        self.workflow_settings = WorkflowSettingsStore(
            self.database.session_factory,
            max_message_bytes=max_message_bytes,
        )
        self.serving_status = ServingStatusService(
            database=self.database,
            models=self.realtime_models,
        )
        self.capacity_settings = CapacitySettingsStore(self.database.session_factory)
        self.capacity = ServingCapacity(CapacityConfig())
        self.capacity_settings.subscribe(self.capacity.apply)
        self.voice_store = VoiceAssetStore(
            self.database.session_factory,
            storage_dir=config.voices.storage_dir,
            max_upload_bytes=config.voices.max_upload_bytes,
        )
        self.tool_registry = ToolRegistry()
        self.qdrant = QdrantGateway(config.qdrant.grpc_target, config.qdrant.http_url)
        self.vectors = VectorService(
            self.database.session_factory,
            self.qdrant,
            self.catalog,
            self.inferencer_pool,
            embedding_limiter=self.capacity.embedding,
            qdrant_limiter=self.capacity.qdrant,
        )

        self.realtime_sessions = self._session_service(self.capacity.realtime_sessions)
        self.webrtc_sessions = self._session_service(self.capacity.webrtc_sessions)

        self.realtime_service = RealtimeTranscriptionService(
            session_service=self.realtime_sessions,
            workflow_settings=self.workflow_settings,
            models=self.realtime_models,
            max_message_bytes=max_message_bytes,
            inferencer_pool=self.inferencer_pool,
            responses_limiter=self.capacity.responses,
            tts_limiter=self.capacity.tts,
            voice_resolver=self.voice_store.resolve,
            tool_registry=self.tool_registry,
            received_audio_cache_dir=(config.received_audio_cache.storage_dir),
            agent_backend_gateway=self.agent_backend_gateway,
        )
        self.grpc_server = AhaGrpcServer(config, self.realtime_service)
        self.webrtc_service = WebRtcCallService(
            config=config.webrtc,
            session_service=self.webrtc_sessions,
            workflow_settings=self.workflow_settings,
            models=self.realtime_models,
            max_message_bytes=max_message_bytes,
            inferencer_pool=self.inferencer_pool,
            responses_limiter=self.capacity.responses,
            tts_limiter=self.capacity.tts,
            voice_resolver=self.voice_store.resolve,
            tool_registry=self.tool_registry,
            received_audio_cache_dir=(config.received_audio_cache.storage_dir),
            agent_backend_gateway=self.agent_backend_gateway,
        )
        self.http_app = create_http_app(
            catalog=self.catalog,
            agent_backends=self.agent_backends,
            workflows=self.audio_workflows,
            voice_agents=self.voice_agents,
            models=self.realtime_models,
            serving_status=self.serving_status,
            workflow_settings=self.workflow_settings,
            capacity_settings=self.capacity_settings,
            capacity=self.capacity,
            realtime_service=self.realtime_service,
            webrtc_service=self.webrtc_service,
            max_message_bytes=max_message_bytes,
            voice_store=self.voice_store,
            vectors=self.vectors,
            qdrant_config=config.qdrant,
        )
        self._closed = False

    def _session_service(self, session_limiter) -> AudioWorkflowSessionService:
        return AudioWorkflowSessionService(
            inferencer_pool=self.inferencer_pool,
            workflow_settings=self.workflow_settings,
            session_limiter=session_limiter,
            asr_limiter=self.capacity.asr,
            embedding_limiter=self.capacity.embedding,
            vectors=self.vectors,
        )

    async def start(self) -> None:
        try:
            await self.database.ping()
            await self.workflow_settings.initialize()
            initial_capacity = await self.capacity_settings.initialize()
            await self.capacity.apply(initial_capacity)
            await self.audio_workflows.initialize()
            await self.voice_agents.initialize()
            await self.grpc_server.start()
            self.engine_health.start()
            self.agent_backend_health.start()
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.grpc_server.stop()
        await self.engine_health.close()
        await self.agent_backend_health.close()
        await self.webrtc_service.close()
        await self.inferencer_pool.close()
        await self.agent_backend_gateway.close()
        await self.qdrant.close()
        await self.database.close()
        logger.info("Audio Harness 已停止")
