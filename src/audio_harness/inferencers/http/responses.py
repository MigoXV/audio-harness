from __future__ import annotations

import asyncio
import os
from typing import Any

from openai import AsyncOpenAI

from audio_harness.assets import EngineSnapshot
from audio_harness.domain import (
    EngineConfigurationInvalidError,
    EngineUnavailableError,
)


class ResponsesInferencer:
    def __init__(self, snapshot: EngineSnapshot) -> None:
        credential_env = snapshot.credential_env
        api_key = snapshot.api_key
        if not api_key and credential_env:
            api_key = os.environ.get(credential_env)
        if not api_key:
            raise EngineConfigurationInvalidError(
                "Responses 引擎缺少 API Key"
            )
        base_url = snapshot.target.rstrip("/")
        if base_url.endswith("/responses"):
            base_url = base_url[: -len("/responses")]
        self.snapshot = snapshot
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=snapshot.request_timeout_seconds or 180.0,
        )

    async def ready(self) -> None:
        return None

    async def probe(self) -> None:
        try:
            await asyncio.wait_for(
                self.client.models.list(),
                timeout=self.snapshot.connect_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise EngineUnavailableError(
                "Responses 连通性校验超时"
            ) from exc

    async def stream_response(self, **kwargs: Any) -> Any:
        kwargs["stream"] = True
        kwargs["store"] = False
        return await self.client.responses.create(**kwargs)

    async def close(self) -> None:
        await self.client.close()
