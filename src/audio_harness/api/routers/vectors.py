from __future__ import annotations

import base64
import json
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Response, UploadFile, status

from audio_harness.configs.app import QdrantConfig
from audio_harness.schemas.vectors import (
    AudioSpeakerSearchRead, DestroyRequest, InfrastructureRead, PayloadReplace, ScrollRequest,
    SpeakerMatchRead,
    VadAudioPreviewRead, VectorSpaceCreate, VectorSpaceList, VectorSpaceRead,
)
from audio_harness.workflows.models import FireRedPostprocessingOptions
from audio_harness.vectors.models import VectorSpaceData
from audio_harness.vectors.service import VectorService


def _space(value: VectorSpaceData) -> VectorSpaceRead:
    return VectorSpaceRead.model_validate({**value.__dict__, "vectors": [item.__dict__ for item in value.vectors]})


def _json_object(value: str, field: str) -> dict[str, Any]:
    try: parsed = json.loads(value)
    except json.JSONDecodeError as exc: raise ValueError(f"{field} 不是合法 JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict): raise ValueError(f"{field} 必须是 JSON object")
    return parsed


def create_vector_router(service: VectorService, config: QdrantConfig) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["vectors"])

    @router.get("/vector-infrastructure", response_model=InfrastructureRead)
    async def infrastructure() -> InfrastructureRead:
        configured, ready = await service.infrastructure()
        return InfrastructureRead(configured=configured, ready=ready, grpc_target=config.grpc_target, http_url=config.http_url)

    @router.get("/vector-spaces", response_model=VectorSpaceList)
    async def list_spaces() -> VectorSpaceList:
        return VectorSpaceList(items=[_space(item) for item in await service.list_spaces()])

    @router.post("/vector-spaces", response_model=VectorSpaceRead, status_code=status.HTTP_201_CREATED)
    async def create_space(payload: VectorSpaceCreate) -> VectorSpaceRead:
        return _space(await service.create_space(payload.model_dump()))

    @router.get("/vector-spaces/{collection_name}", response_model=VectorSpaceRead)
    async def get_space(collection_name: str) -> VectorSpaceRead:
        return _space(await service.get_space(collection_name))

    @router.post("/vector-spaces/{collection_name}/destroy", status_code=status.HTTP_204_NO_CONTENT)
    async def destroy(collection_name: str, payload: DestroyRequest) -> Response:
        await service.destroy(collection_name, payload.confirmation); return Response(status_code=204)

    @router.post("/vector-spaces/{collection_name}/points/audio", status_code=status.HTTP_201_CREATED)
    async def register_audio(
        collection_name: str, audio: Annotated[UploadFile, File()],
        embeddings_json: Annotated[str, Form()], payload_json: Annotated[str, Form()] = "{}",
        point_id: Annotated[str | None, Form()] = None,
        speaker_id: Annotated[str | None, Form()] = None,
        channel_id: Annotated[str | None, Form()] = None,
        captured_at: Annotated[int | None, Form()] = None,
        quality_score: Annotated[float | None, Form()] = None,
    ) -> dict[str, str]:
        configs = json.loads(embeddings_json)
        if not isinstance(configs, list): raise ValueError("embeddings_json 必须是数组")
        value = await service.register_audio(
            collection_name, await audio.read(), configs,
            _json_object(payload_json, "payload_json"), point_id,
            speaker_id=speaker_id, channel_id=channel_id, captured_at=captured_at,
            quality_score=quality_score,
        )
        return {"point_id": value}

    @router.post("/vector-spaces/{collection_name}/search/audio", response_model=AudioSpeakerSearchRead)
    async def search_audio(
        collection_name: str, audio: Annotated[UploadFile, File()],
        vector_name: Annotated[str, Form()], engine_id: Annotated[str, Form()],
        algorithm: Annotated[str, Form()], normalize: Annotated[bool, Form()] = True,
        top_k: Annotated[int, Form()] = 10, score_threshold: Annotated[float | None, Form()] = None,
        filter_json: Annotated[str, Form()] = "{}",
    ) -> AudioSpeakerSearchRead:
        if not 1 <= top_k <= 100: raise ValueError("top_k 必须位于 1 到 100")
        vector, groups = await service.search_audio(collection_name, await audio.read(), vector_name=vector_name, engine_id=engine_id, algorithm=algorithm, normalize=normalize, top_k=top_k, score_threshold=score_threshold, filter_json=_json_object(filter_json, "filter_json"))
        return AudioSpeakerSearchRead(
            embedding=list(vector),
            groups=[SpeakerMatchRead.model_validate({
                "speaker_id": item.speaker_id,
                "score": item.score,
                "observation": {
                    "point_id": item.observation.point_id,
                    "payload": item.observation.payload,
                },
            }) for item in groups],
        )

    @router.post(
        "/vector-spaces/{collection_name}/search/audio/vad-preview",
        response_model=VadAudioPreviewRead,
    )
    async def preview_vad_audio(
        collection_name: str,
        audio: Annotated[UploadFile, File()],
        vad_engine_id: Annotated[str, Form()],
        vad_postprocessing_json: Annotated[str | None, Form()] = None,
    ) -> VadAudioPreviewRead:
        postprocessing = None
        if vad_postprocessing_json:
            parsed = FireRedPostprocessingOptions.model_validate(
                _json_object(vad_postprocessing_json, "vad_postprocessing_json")
            )
            postprocessing = {
                "processor": parsed.processor,
                "version": parsed.version,
                "parameters": parsed.parameters.model_dump(),
            }
        arguments: dict[str, Any] = {"vad_engine_id": vad_engine_id}
        if postprocessing is not None:
            arguments["postprocessing"] = postprocessing
        wav, duration_ms = await service.preview_vad_audio(
            collection_name, await audio.read(), **arguments
        )
        return VadAudioPreviewRead(
            audio_base64=base64.b64encode(wav).decode("ascii"),
            duration_ms=duration_ms,
        )

    @router.post("/vector-spaces/{collection_name}/points/scroll")
    async def scroll(collection_name: str, payload: ScrollRequest) -> dict[str, Any]:
        await service.get_space(collection_name)
        items, offset = await service.gateway.scroll(collection_name, limit=payload.limit, offset=payload.offset, filter_json=payload.filter, with_vectors=payload.with_vectors)
        return {"items": items, "next_offset": offset}

    @router.put("/vector-spaces/{collection_name}/points/{point_id}/payload", status_code=204)
    async def replace_payload(collection_name: str, point_id: str, payload: PayloadReplace) -> Response:
        await service.replace_payload(collection_name, point_id, payload.payload); return Response(status_code=204)

    @router.delete("/vector-spaces/{collection_name}/points/{point_id}", status_code=204)
    async def delete_point(collection_name: str, point_id: str) -> Response:
        await service.get_space(collection_name); await service.gateway.delete_point(collection_name, point_id); return Response(status_code=204)

    return router
