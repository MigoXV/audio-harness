"""WebSocket adapter for the shared Realtime application service."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from audio_harness.errors import CapacityExceededError
from .core import (
    RealtimeConnection,
    RealtimeConnectionMode,
    RealtimeConnectionRejected,
    RealtimeOptions,
    RealtimeService,
    _OnlinePcmResampler,
)
from .json_codec import InvalidAudioEncoding, decode_client_event, encode_server_event


class RealtimeTranscriptionService(RealtimeService):
    """Existing WS entrypoint; all session semantics live in RealtimeService."""

    async def handle(
        self,
        websocket: WebSocket,
        *,
        mode: RealtimeConnectionMode = RealtimeConnectionMode.TRANSCRIPTION,
        model: str | None = None,
        diagnostics_enabled: bool = False,
        aha_version: str | None = None,
    ) -> None:
        connection = None
        tasks: list[asyncio.Task[Any]] = []
        try:
            connection = await self.open(
                RealtimeOptions(
                    intent=(
                        "transcription"
                        if mode is RealtimeConnectionMode.TRANSCRIPTION
                        else None
                    ),
                    model=model,
                    diagnostics_enabled=diagnostics_enabled,
                    aha_version=aha_version,
                )
            )
            await websocket.accept()
            tasks = [
                asyncio.create_task(self._receive(websocket, connection)),
                asyncio.create_task(self._send(websocket, connection)),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except RealtimeConnectionRejected as exc:
            if exc.event is not None:
                await websocket.accept()
                await websocket.send_json(encode_server_event(exc.event))
            codes = {
                "invalid_argument": 4400,
                "not_found": 4404,
                "failed_precondition": 4409,
                "internal": 1011,
            }
            await websocket.close(code=codes[exc.code], reason=str(exc))
        except CapacityExceededError:
            await websocket.close(code=4429, reason="Session capacity exceeded")
        except WebSocketDisconnect:
            pass
        finally:
            if connection is not None:
                connection.close_output()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if connection is not None:
                await connection.close()

    async def _send(self, websocket: WebSocket, connection: RealtimeConnection) -> None:
        async for event in connection.events():
            await websocket.send_json(encode_server_event(event))

    async def _receive(
        self, websocket: WebSocket, connection: RealtimeConnection
    ) -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            raw = message.get("text")
            if raw is None:
                raw = message.get("bytes")
            if not isinstance(raw, (str, bytes)):
                await connection.send_error(
                    message="Realtime 事件必须是 JSON 文本", code="invalid_event"
                )
                continue
            size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
            if size > self.max_message_bytes:
                await connection.send_error(
                    message="Realtime 事件超过 max_message_bytes",
                    code="message_too_large",
                )
                continue
            payload: Any = None
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("Realtime 事件必须是 JSON 对象")
                event = decode_client_event(payload)
            except (ValueError, UnicodeDecodeError, ValidationError) as exc:
                await connection.send_error(
                    message=str(exc),
                    code=(
                        "invalid_audio_base64"
                        if isinstance(exc, InvalidAudioEncoding)
                        else "invalid_event"
                    ),
                    client_event_id=(
                        payload.get("event_id") if isinstance(payload, dict) else None
                    ),
                )
                continue
            await connection.handle_event(event)
