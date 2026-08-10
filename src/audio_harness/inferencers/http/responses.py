from __future__ import annotations

import asyncio
import os
from typing import Any

from openai import AsyncOpenAI

from audio_harness.assets import EngineSnapshot
from audio_harness.domain import (
    EngineConfigurationInvalidError,
    EngineDeadlineError,
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

    async def list_models(self) -> tuple[str, ...]:
        try:
            page = await asyncio.wait_for(
                self.client.models.list(),
                timeout=self.snapshot.connect_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise EngineDeadlineError(
                "Responses 模型列表读取超时"
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise EngineUnavailableError(
                "Responses 模型列表读取失败"
            ) from exc
        return tuple(
            sorted(
                {
                    item.id.strip()
                    for item in page.data
                    if item.id.strip()
                }
            )
        )

    async def probe(self) -> None:
        await self.list_models()

    async def stream_response(self, **kwargs: Any) -> Any:
        kwargs["stream"] = True
        kwargs["store"] = False
        return await self.client.responses.create(**kwargs)

    async def close(self) -> None:
        await self.client.close()
