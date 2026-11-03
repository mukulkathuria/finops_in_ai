"""Outbox dispatcher (spec section 4, step 12): delivers outbox events to each org's webhook.

At least once: a message is leased, delivered outside any transaction, then marked delivered,
retried with exponential backoff, or failed for good after `max_attempts`. Receivers dedupe on
the `X-FinOps-Delivery` id and verify `X-FinOps-Signature` (see gateway/domain/webhooks.py).
SSRF guard: every resolved address of the host must be public unless `allow_insecure` (dev).
"""

import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import httpx2
import orjson

from gateway.adapters.postgres.database import Database
from gateway.adapters.postgres.webhooks import OutboxMessage, WebhookRepository, webhook_aad
from gateway.adapters.security.envelope import DecryptionError, Envelope
from gateway.domain.webhooks import is_public_address, sign

log = logging.getLogger(__name__)
_LEASE_S: Final = 300.0
_MAX_BACKOFF_S: Final = 6 * 3600.0


@dataclass(frozen=True, slots=True)
class DispatchReport:
    delivered: int
    retried: int
    failed: int


class _Permanent(Exception):
    """A delivery that can never succeed as configured (no retry)."""


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class OutboxDispatcher:
    def __init__(
        self,
        db: Database,
        envelope: Envelope,
        http: httpx2.AsyncClient,
        *,
        allow_insecure: bool,
        batch: int = 50,
        max_attempts: int = 10,
    ) -> None:
        self._db = db
        self._envelope = envelope
        self._http = http
        self._allow_insecure = allow_insecure
        self._batch = batch
        self._max_attempts = max_attempts
        self._repo = WebhookRepository()

    async def run_once(self) -> DispatchReport:
        async with self._db.transaction() as conn:
            messages = await self._repo.lease(conn, limit=self._batch, lease_s=_LEASE_S)
        delivered = retried = failed = 0
        for message in messages:
            outcome = await self._deliver(message)
            delivered += outcome == "delivered"
            retried += outcome == "retried"
            failed += outcome == "failed"
        return DispatchReport(delivered, retried, failed)

    async def _endpoint(self, message: OutboxMessage) -> tuple[str, str] | None:
        async with self._db.transaction() as conn:
            sealed = await self._repo.sealed(conn, message.org_id)
        if sealed is None:
            return None
        try:
            stored = orjson.loads(self._envelope.open(sealed, webhook_aad(message.org_id)))
        except DecryptionError as exc:
            raise _Permanent(
                "the webhook secret cannot be decrypted with the current keys"
            ) from exc
        return str(stored["url"]), str(stored["secret"])

    async def _check_host(self, url: str) -> None:
        if self._allow_insecure:
            return
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port or 443
        try:
            addresses = await _resolve(host, port)
        except OSError as exc:
            raise ConnectionError(f"cannot resolve {host}") from exc
        if not addresses or not all(is_public_address(a) for a in addresses):
            raise _Permanent(f"{host} resolves to a non-public address")

    async def _deliver(self, message: OutboxMessage) -> str:
        try:
            endpoint = await self._endpoint(message)
            if endpoint is None:
                await self._mark(message, "delivered", note="no webhook configured")
                return "delivered"
            url, secret = endpoint
            await self._check_host(url)
            body = orjson.dumps(
                {
                    "id": str(message.id),
                    "type": message.topic,
                    "created_at": message.created_at.isoformat(),
                    "data": message.payload,
                }
            )
            timestamp = int(time.time())
            response = await self._http.post(
                url,
                content=body,
                headers={
                    "content-type": "application/json",
                    "user-agent": "finops-gateway-webhooks/1",
                    "x-finops-event": message.topic,
                    "x-finops-delivery": str(message.id),
                    "x-finops-timestamp": str(timestamp),
                    "x-finops-signature": sign(secret, timestamp, body),
                },
                timeout=httpx2.Timeout(10.0, connect=5.0),
                follow_redirects=False,
            )
            if 200 <= response.status_code < 300:
                await self._mark(message, "delivered")
                return "delivered"
            raise ConnectionError(f"HTTP {response.status_code}")
        except _Permanent as exc:
            await self._mark(message, "failed", note=str(exc))
            return "failed"
        except (ConnectionError, httpx2.HTTPError, OSError) as exc:
            reason = str(exc) or type(exc).__name__
            if message.attempts >= self._max_attempts:
                await self._mark(message, "failed", note=f"gave up: {reason}")
                return "failed"
            delay = min(30.0 * 2 ** (message.attempts - 1), _MAX_BACKOFF_S)
            async with self._db.transaction() as conn:
                await self._repo.retry_later(conn, message.id, delay_s=delay, error=reason)
            log.warning(
                "webhook delivery failed; will retry",
                extra={"delivery": str(message.id), "attempt": message.attempts, "error": reason},
            )
            return "retried"

    async def _mark(self, message: OutboxMessage, state: str, note: str | None = None) -> None:
        async with self._db.transaction() as conn:
            if state == "delivered":
                await self._repo.delivered(conn, message.id, note)
            else:
                await self._repo.failed(conn, message.id, error=note or "failed")
