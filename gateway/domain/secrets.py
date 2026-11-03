"""Opaque bearer secrets (invitation links, refresh tokens, API keys): generate and digest.

Only the SHA-256 digest is stored. The secrets carry 256 bits of entropy, so a fast hash is
correct here; slow password hashing would add latency without adding security.
"""

import hashlib
import secrets


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()
