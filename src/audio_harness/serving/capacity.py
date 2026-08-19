from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from audio_harness.configs import CapacityConfig
from audio_harness.inferencers.limits import ResizableConcurrencyLimiter
from audio_harness.serving.capacity_settings import CapacitySettingsSnapshot
from audio_harness.sessions.limits import ResizableSessionLimiter


class ServingCapacity:
    def __init__(self, config: CapacityConfig) -> None:
        self.started_at = datetime.now(timezone.utc)
        self.realtime_sessions = ResizableSessionLimiter(
            config.realtime.max_sessions
        )
        self.webrtc_sessions = ResizableSessionLimiter(
            config.webrtc.max_sessions
        )
        self.asr = ResizableConcurrencyLimiter(
            config.max_asr_concurrency
        )
        self.embedding = ResizableConcurrencyLimiter(
            config.max_embedding_concurrency
        )
        self.qdrant = ResizableConcurrencyLimiter(
            config.max_qdrant_concurrency
        )
        self.responses = ResizableConcurrencyLimiter(
            config.max_responses_concurrency
        )
        self.tts = ResizableConcurrencyLimiter(
            config.max_tts_concurrency
        )

    async def apply(self, snapshot: CapacitySettingsSnapshot) -> None:
        await asyncio.gather(
            self.realtime_sessions.resize(
                snapshot.config.realtime.max_sessions
            ),
            self.webrtc_sessions.resize(
                snapshot.config.webrtc.max_sessions
            ),
            self.asr.resize(snapshot.config.max_asr_concurrency),
            self.embedding.resize(snapshot.config.max_embedding_concurrency),
            self.qdrant.resize(snapshot.config.max_qdrant_concurrency),
            self.responses.resize(
                snapshot.config.max_responses_concurrency
            ),
            self.tts.resize(snapshot.config.max_tts_concurrency),
        )
