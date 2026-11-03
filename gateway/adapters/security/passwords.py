"""Argon2id password hashing off the event loop (US-01, spec section 10).

argon2-cffi defaults are the RFC 9106 low-memory profile (t=3, m=64 MiB, p=4). Hashing runs in a
worker thread behind a semaphore so a login flood cannot exhaust CPU or memory, and misses are
verified against a dummy hash so response time does not reveal whether an account exists.
"""

import asyncio
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class Argon2PasswordHasher:
    def __init__(self, *, max_concurrency: int = 4) -> None:
        self._hasher = PasswordHasher()
        self._slots = asyncio.Semaphore(max_concurrency)
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(24))

    async def hash(self, password: str) -> str:
        async with self._slots:
            return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password_hash: str | None, password: str) -> bool:
        """Constant-shape check: an unknown user costs the same as a wrong password."""
        target = password_hash or self._dummy_hash
        async with self._slots:
            try:
                await asyncio.to_thread(self._hasher.verify, target, password)
            except VerifyMismatchError, VerificationError, InvalidHashError:
                return False
        return password_hash is not None

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)
