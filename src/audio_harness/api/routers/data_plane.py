from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, Request, Response, WebSocket, status
from openai.types.realtime import (
    RealtimeSessionCreateRequest,
    RealtimeTranscriptionSessionCreateRequest,
)
from pydantic import ValidationError

from audio_harness.errors import CapacityExceededError
from audio_harness.serving.realtime import (
    RealtimeConnectionMode,
    RealtimeTranscriptionService,
)
from audio_harness.serving.webrtc import WebRtcCallService


def _error(status_code: int, code: str, message: str) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": None}},
    )


def create_data_plane_router(
    *,
    realtime: RealtimeTranscriptionService,
    webrtc: WebRtcCallService,
    max_message_bytes: int,
) -> APIRouter:
    router = APIRouter(tags=["realtime"])

    @router.websocket("/v1/realtime")
    async def realtime_socket(
        websocket: WebSocket,
        intent: str | None = Query(default=None, min_length=1),
        model: str | None = Query(default=None, min_length=1),
        diagnostics: bool = Query(default=False),
        x_aha: str | None = Query(default=None),
    ) -> None:
        if (intent is None) == (model is None):
            await websocket.close(
                code=4400,
                reason="Specify exactly one of intent or model",
            )
            return
        if intent is not None and intent != "transcription":
            await websocket.close(
                code=4400,
                reason="Unsupported realtime intent",
            )
            return
        if x_aha not in (None, "v1"):
            await websocket.close(code=4400, reason="Unsupported x_aha version")
            return
        await realtime.handle(
            websocket,
            mode=(
                RealtimeConnectionMode.TRANSCRIPTION
                if intent is not None
                else RealtimeConnectionMode.REALTIME
            ),
            model=model,
            diagnostics_enabled=diagnostics,
            aha_version=x_aha,
        )

    @router.post("/v1/realtime/calls")
    async def create_call(
        request: Request,
        diagnostics: bool = Query(default=False),
        model: str | None = Query(default=None, min_length=1),
        x_aha: str | None = Query(default=None),
    ) -> Response:
        if x_aha not in (None, "v1"):
            return _error(
                400,
                "unsupported_x_aha_version",
                "只支持 x_aha=v1",
            )
        try:
            form = await request.form()
            sdp_part = form.get("sdp")
            if sdp_part is None:
                return _error(400, "invalid_sdp", "缺少 sdp 表单字段")
            if hasattr(sdp_part, "read"):
                offer = await sdp_part.read(max_message_bytes + 1)
            elif isinstance(sdp_part, str):
                offer = sdp_part.encode("utf-8")
            else:
                return _error(400, "invalid_sdp", "sdp 必须是文本或文件")
            if len(offer) > max_message_bytes:
                return _error(400, "sdp_too_large", "sdp 超过 max_message_bytes")
            offer_sdp = offer.decode("utf-8")
            session_part = form.get("session")
            if hasattr(session_part, "read"):
                session_bytes = await session_part.read(max_message_bytes + 1)
                if len(session_bytes) > max_message_bytes:
                    return _error(400, "session_too_large", "session 超过 max_message_bytes")
                session_json = session_bytes.decode("utf-8")
            elif session_part is None or isinstance(session_part, str):
                session_json = session_part
            else:
                return _error(400, "invalid_session", "session 必须是 JSON 文本或文件")
            payload = json.loads(session_json) if session_json is not None else None
            if payload is None:
                session_config = None
            elif payload.get("type") == "realtime":
                session_config = RealtimeSessionCreateRequest.model_validate(payload)
            else:
                session_config = RealtimeTranscriptionSessionCreateRequest.model_validate(payload)
            selected_model = model
            if selected_model is None and payload is not None:
                if payload.get("type") == "realtime":
                    selected_model = payload.get("model")
                else:
                    audio_input = ((payload.get("audio") or {}).get("input") or {})
                    selected_model = (audio_input.get("transcription") or {}).get("model")
            answer = await webrtc.create_call(
                offer_sdp=offer_sdp,
                session=session_config,
                model=selected_model or "gpt-4o-transcribe",
                diagnostics_enabled=diagnostics,
                aha_version=x_aha,
            )
        except UnicodeDecodeError:
            return _error(400, "invalid_encoding", "sdp 和 session 必须是 UTF-8 文本")
        except (json.JSONDecodeError, ValidationError) as exc:
            return _error(400, "invalid_session", f"session 配置不合法: {exc}")
        except CapacityExceededError as exc:
            return _error(429, "capacity_exceeded", str(exc))
        except ValueError as exc:
            return _error(400, "invalid_webrtc_offer", str(exc))
        except asyncio.TimeoutError:
            return _error(408, "negotiation_timeout", "WebRTC 协商超时")
        return Response(
            content=answer.sdp,
            status_code=status.HTTP_201_CREATED,
            media_type="application/sdp",
            headers={"Location": f"/v1/realtime/calls/{answer.call_id}"},
        )

    @router.post("/v1/realtime/calls/{call_id}/hangup")
    async def hangup(call_id: str) -> Response:
        await webrtc.hangup(call_id)
        return Response(status_code=status.HTTP_200_OK)

    return router
