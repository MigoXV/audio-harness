from __future__ import annotations

from dataclasses import dataclass

from audio_harness.assets import EngineApi, EngineProtocol, EngineSnapshot
from audio_harness.domain import EngineConfigurationInvalidError
from audio_harness.inferencers.contracts import ManagedInferencer
from audio_harness.inferencers.grpc.factory import GrpcInferencerFactory
from audio_harness.inferencers.http import ResponsesInferencer


@dataclass(frozen=True)
class DefaultInferencerFactory:
    min_chunk_bytes: int
    max_message_bytes: int

    def create(self, snapshot: EngineSnapshot) -> ManagedInferencer:
        if (
            snapshot.protocol is EngineProtocol.HTTPS
            and snapshot.api is EngineApi.RESPONSES
        ):
            return ResponsesInferencer(snapshot)
        if snapshot.protocol is EngineProtocol.GRPC:
            return GrpcInferencerFactory(
                min_chunk_bytes=self.min_chunk_bytes,
                max_message_bytes=self.max_message_bytes,
            ).create(snapshot)
        raise EngineConfigurationInvalidError(
            f"不支持的引擎协议/API: "
            f"{snapshot.protocol.value}/{snapshot.api.value}"
        )
