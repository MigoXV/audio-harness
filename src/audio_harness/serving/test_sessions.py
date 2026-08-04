from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from time import monotonic


TEST_SESSION_COOKIE = "audio_harness_test_session"


@dataclass(frozen=True)
class TestSessionCredential:
    __test__ = False

    id: str
    token: str
    workflow_id: str
    model: str
    protocol: str
    expires_at: datetime
    expires_monotonic: float


class TestSessionBroker:
    """进程内、短时、单次握手使用的控制台测试凭据。"""

    __test__ = False

    def __init__(self, ttl_seconds: int = 60) -> None:
        self._ttl_seconds = ttl_seconds
        self._credentials: dict[str, TestSessionCredential] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        workflow_id: str,
        model: str,
        protocol: str,
    ) -> TestSessionCredential:
        now = datetime.now(timezone.utc)
        credential = TestSessionCredential(
            id=f"test_{secrets.token_hex(12)}",
            token=secrets.token_urlsafe(32),
            workflow_id=workflow_id,
            model=model,
            protocol=protocol,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
            expires_monotonic=monotonic() + self._ttl_seconds,
        )
        async with self._lock:
            self._purge_expired()
            self._credentials[credential.token] = credential
        return credential

    def _purge_expired(self) -> None:
        now = monotonic()
        for token, credential in list(self._credentials.items()):
            if credential.expires_monotonic <= now:
                del self._credentials[token]

    async def consume(
        self,
        token: str | None,
        *,
        model: str,
        protocol: str,
    ) -> bool:
        if not token:
            return False
        async with self._lock:
            self._purge_expired()
            credential = self._credentials.pop(token, None)
            if credential is None:
                return False
            return credential.model == model and credential.protocol == protocol
