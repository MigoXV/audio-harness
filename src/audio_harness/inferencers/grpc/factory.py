from __future__ import annotations

from dataclasses import dataclass

from audio_harness.assets import EngineApi, EngineProtocol, EngineSnapshot
from audio_harness.config import EngineEndpoint
from audio_harness.domain import EngineBindingInvalidError
from audio_harness.inferencers.contracts import ManagedInferencer
from audio_harness.inferencers.grpc.asr import GrpcAsrInferencer
from audio_harness.inferencers.grpc.vad import GrpcVadInferencer


@dataclass(frozen=True)
class GrpcInferencerFactory:
    min_chunk_bytes: int
    max_message_bytes: int

    def create(self, snapshot: EngineSnapshot) -> ManagedInferencer:
        if snapshot.protocol is not EngineProtocol.GRPC:
            raise EngineBindingInvalidError(
                f"gRPC 工厂不支持协议: {snapshot.protocol.value}"
            )
        endpoint = EngineEndpoint(
            target=snapshot.target,
            connect_timeout_seconds=snapshot.connect_timeout_seconds,
            request_timeout_seconds=snapshot.request_timeout_seconds,
        )
        if snapshot.api is EngineApi.UX_SPEECH:
            return GrpcAsrInferencer(
                endpoint,
                min_chunk_bytes=self.min_chunk_bytes,
                max_message_bytes=self.max_message_bytes,
            )
        if snapshot.api is EngineApi.UX_VAD:
            return GrpcVadInferencer(
                endpoint,
                max_message_bytes=self.max_message_bytes,
            )
        raise EngineBindingInvalidError(
            f"gRPC 工厂不支持能力接口: {snapshot.api.value}"
        )
