"""Webhook endpoints and the outbox (spec section 4: events leave through a transactional outbox).

Delivery is at least once: the worker leases due rows (moving `available_at` forward), delivers
outside any transaction and then records the outcome; a crashed worker's lease simply expires.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import orjson
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one
from gateway.adapters.security.envelope import Sealed


def webhook_aad(org_id: UUID) -> bytes:
    """Binds the sealed URL and secret to their org: a copied ciphertext fails to decrypt."""
    return f"webhook:{org_id}".encode()


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    org_id: UUID
    url_hint: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: UUID
    org_id: UUID
    topic: str
    payload: dict[str, Any]
    created_at: datetime
    attempts: int


@dataclass(frozen=True, slots=True)
class Delivery:
    id: UUID
    topic: str
    created_at: datetime
    attempts: int
    delivered_at: datetime | None
    failed_at: datetime | None
    last_error: str | None


_UPSERT: Final = """
INSERT INTO webhook_endpoints AS w (org_id, url_hint, ciphertext, wrapped_key, key_id, created_by)
VALUES (app_current_org(), :hint, :ct, :wk, :kid, :actor)
ON CONFLICT (org_id) DO UPDATE
   SET url_hint = excluded.url_hint, ciphertext = excluded.ciphertext,
       wrapped_key = excluded.wrapped_key, key_id = excluded.key_id
RETURNING org_id, url_hint, created_at, updated_at, (old.org_id IS NULL) AS created
"""

_SEALED: Final = """
SELECT org_id, url_hint, created_at, updated_at, ciphertext, wrapped_key, key_id
  FROM webhook_endpoints WHERE org_id = :org
"""

# Lease due messages: a message is retried only after its lease (or backoff) passes.
_LEASE: Final = """
WITH due AS (
  SELECT id FROM outbox
   WHERE delivered_at IS NULL AND failed_at IS NULL AND available_at <= now()
   ORDER BY available_at
   LIMIT :limit
     FOR UPDATE SKIP LOCKED
)
UPDATE outbox o
   SET attempts = o.attempts + 1, available_at = now() + make_interval(secs => :lease)
  FROM due WHERE o.id = due.id
RETURNING o.id, o.org_id, o.topic, o.payload, o.created_at, o.attempts
"""


class WebhookRepository:
    async def upsert(
        self, conn: AsyncConnection, *, hint: str, sealed: Sealed, actor: UUID
    ) -> tuple[WebhookEndpoint, bool]:
        row = await fetch_one(
            conn,
            _UPSERT,
            {
                "hint": hint,
                "ct": sealed.ciphertext,
                "wk": sealed.wrapped_key,
                "kid": sealed.key_id,
                "actor": actor,
            },
        )
        if row is None:  # pragma: no cover - an upsert always returns its row
            raise RuntimeError("webhook upsert returned no row")
        return _endpoint(row), bool(row["created"])

    async def get(self, conn: AsyncConnection, org_id: UUID) -> WebhookEndpoint | None:
        row = await fetch_one(conn, _SEALED, {"org": org_id})
        return _endpoint(row) if row else None

    async def sealed(self, conn: AsyncConnection, org_id: UUID) -> Sealed | None:
        row = await fetch_one(conn, _SEALED, {"org": org_id})
        if row is None:
            return None
        return Sealed(bytes(row["ciphertext"]), bytes(row["wrapped_key"]), str(row["key_id"]))

    async def delete(self, conn: AsyncConnection) -> bool:
        row = await fetch_one(
            conn,
            "DELETE FROM webhook_endpoints WHERE org_id = app_current_org() RETURNING org_id",
        )
        return row is not None

    async def deliveries(self, conn: AsyncConnection, *, limit: int = 100) -> list[Delivery]:
        rows = await fetch_all(
            conn,
            "SELECT id, topic, created_at, attempts, delivered_at, failed_at, last_error"
            " FROM outbox ORDER BY created_at DESC LIMIT :limit",
            {"limit": limit},
        )
        return [Delivery(**dict(r)) for r in rows]

    async def lease(
        self, conn: AsyncConnection, *, limit: int, lease_s: float
    ) -> list[OutboxMessage]:
        rows = await fetch_all(conn, _LEASE, {"limit": limit, "lease": lease_s})
        return [
            OutboxMessage(
                id=r["id"],
                org_id=r["org_id"],
                topic=r["topic"],
                payload=r["payload"]
                if isinstance(r["payload"], dict)
                else orjson.loads(r["payload"]),
                created_at=r["created_at"],
                attempts=int(r["attempts"]),
            )
            for r in rows
        ]

    async def delivered(
        self, conn: AsyncConnection, message_id: UUID, note: str | None = None
    ) -> None:
        await fetch_one(
            conn,
            "UPDATE outbox SET delivered_at = now(), last_error = :note"
            " WHERE id = :id RETURNING id",
            {"id": message_id, "note": note},
        )

    async def retry_later(
        self, conn: AsyncConnection, message_id: UUID, *, delay_s: float, error: str
    ) -> None:
        await fetch_one(
            conn,
            "UPDATE outbox SET available_at = now() + make_interval(secs => :delay),"
            " last_error = :error WHERE id = :id RETURNING id",
            {"id": message_id, "delay": delay_s, "error": error[:500]},
        )

    async def failed(self, conn: AsyncConnection, message_id: UUID, *, error: str) -> None:
        await fetch_one(
            conn,
            "UPDATE outbox SET failed_at = now(), last_error = :error WHERE id = :id RETURNING id",
            {"id": message_id, "error": error[:500]},
        )


def _endpoint(row: RowMapping) -> WebhookEndpoint:
    return WebhookEndpoint(
        org_id=row["org_id"],
        url_hint=row["url_hint"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
