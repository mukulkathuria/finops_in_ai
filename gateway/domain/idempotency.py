"""Idempotency keys (spec section 6): one client key, one charge."""

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from gateway.domain.errors import InvalidInput

_KEY: Final = re.compile(r"[\x21-\x7e]{1,255}")


@dataclass(frozen=True, slots=True)
class Idempotency:
    caller: str  # the credential the key belongs to: equal keys of two callers never meet
    key: str
    fingerprint: bytes  # SHA-256 of route and body: a key reused for another request is refused

    @classmethod
    def from_header(
        cls, header: str | None, *, caller: str, route: str, body: bytes
    ) -> Idempotency | None:
        if header is None:
            return None
        if not _KEY.fullmatch(header):
            raise InvalidInput(
                "Idempotency-Key must be 1 to 255 printable ASCII characters",
                code="invalid_idempotency_key",
                param="Idempotency-Key",
            )
        return cls(caller, header, hashlib.sha256(route.encode() + b"\n" + body).digest())
