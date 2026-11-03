"""Idempotency-Key storage (spec section 6). A key is claimed in the transaction that reserves
the budget and completed in the one that settles it, so a stored result always matches exactly
one charge, and a crash in between leaves no result that was never paid for."""

from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_one
from gateway.domain.errors import Conflict, InvalidInput
from gateway.domain.idempotency import Idempotency

# A key held longer than `stale_after_s` belongs to an attempt that died (its budget hold has
# expired as well), so a retry may take it over.
_CLAIM: Final = """
INSERT INTO idempotency_keys AS k (org_id, caller, key, fingerprint)
VALUES (app_current_org(), :caller, :key, :fp)
ON CONFLICT (org_id, caller, key) DO UPDATE
   SET fingerprint = excluded.fingerprint, status = 'in_progress', response = NULL,
       created_at = now()
 WHERE k.status = 'in_progress' AND k.created_at < now() - make_interval(secs => :stale)
RETURNING 1 AS owned
"""

_FIND: Final = """
SELECT fingerprint, status, response FROM idempotency_keys
 WHERE org_id = app_current_org() AND caller = :caller AND key = :key
"""

_COMPLETE: Final = """
UPDATE idempotency_keys SET status = 'completed', response = :response
 WHERE org_id = app_current_org() AND caller = :caller AND key = :key
RETURNING 1
"""

_FORGET: Final = """
DELETE FROM idempotency_keys
 WHERE org_id = app_current_org() AND caller = :caller AND key = :key AND status = 'in_progress'
RETURNING 1
"""

_BUSY: Final = "a request with this Idempotency-Key is still in progress; retry shortly"


class IdempotencyRepository:
    def __init__(self, *, stale_after_s: float) -> None:
        self._stale_after_s = stale_after_s

    async def claim(self, conn: AsyncConnection, idem: Idempotency) -> bytes | None:
        """None: this request now owns the key and runs. Bytes: an earlier attempt with the same
        key and body finished; replay its stored result. Raises while that attempt still runs."""
        owner = {"caller": idem.caller, "key": idem.key}
        for _ in range(2):  # the holder may forget the key between our two statements
            claimed = await fetch_one(
                conn, _CLAIM, {**owner, "fp": idem.fingerprint, "stale": self._stale_after_s}
            )
            if claimed is not None:
                return None
            row = await fetch_one(conn, _FIND, owner)
            if row is None:
                continue
            if bytes(row["fingerprint"]) != idem.fingerprint:
                raise InvalidInput(
                    "this Idempotency-Key was already used for a different request",
                    code="idempotency_key_reused",
                    param="Idempotency-Key",
                )
            if row["status"] != "completed" or row["response"] is None:
                raise Conflict(_BUSY, code="idempotency_in_progress")
            return bytes(row["response"])
        raise Conflict(_BUSY, code="idempotency_in_progress")

    async def complete(self, conn: AsyncConnection, idem: Idempotency, result: bytes) -> None:
        await fetch_one(
            conn, _COMPLETE, {"caller": idem.caller, "key": idem.key, "response": result}
        )

    async def forget(self, conn: AsyncConnection, idem: Idempotency) -> None:
        """A failed or cancelled attempt frees its key: the client may retry it."""
        await fetch_one(conn, _FORGET, {"caller": idem.caller, "key": idem.key})
