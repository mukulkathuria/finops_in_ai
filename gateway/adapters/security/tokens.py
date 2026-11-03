"""EdDSA (Ed25519) access tokens with `kid`, pinned algorithm and a JWKS document (US-01, US-03).

Keys come from settings as base64url-encoded 32-byte values: the current private seed plus any
previous public keys still accepted during a rotation window.
"""

import base64
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from jwt.algorithms import OKPAlgorithm

from gateway.domain.errors import Unauthenticated

ALGORITHM: Final = "EdDSA"
_REQUIRED_CLAIMS: Final = ["exp", "iat", "nbf", "iss", "aud", "sub", "jti"]


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def private_key_from_seed(seed_b64url: str) -> Ed25519PrivateKey:
    seed = b64url_decode(seed_b64url)
    if len(seed) != 32:
        raise ValueError("JWT_SIGNING_KEY must be a base64url-encoded 32-byte Ed25519 seed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def parse_public_keys(spec: str) -> dict[str, Ed25519PublicKey]:
    """`kid:base64url,kid2:base64url` -> {kid: key}."""
    keys: dict[str, Ed25519PublicKey] = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        kid, sep, encoded = item.partition(":")
        if not sep or not kid:
            raise ValueError("JWT_PREVIOUS_PUBLIC_KEYS entries must look like kid:base64url")
        keys[kid] = Ed25519PublicKey.from_public_bytes(b64url_decode(encoded))
    return keys


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: UUID
    org_id: UUID
    token_id: str
    issued_at: int
    expires_at: int


class TokenService:
    def __init__(
        self,
        *,
        signing_key: Ed25519PrivateKey,
        key_id: str,
        issuer: str,
        audience: str,
        access_ttl_seconds: int,
        previous_keys: Mapping[str, Ed25519PublicKey] | None = None,
    ) -> None:
        self._signing_key = signing_key
        self._key_id = key_id
        self._issuer = issuer
        self._audience = audience
        self.access_ttl_seconds = access_ttl_seconds
        self._verification_keys = {**(previous_keys or {}), key_id: signing_key.public_key()}

    def issue_access(self, user_id: UUID, org_id: UUID, *, now: int | None = None) -> str:
        issued = int(time.time()) if now is None else now
        claims = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": str(user_id),
            "org": str(org_id),
            "typ": "access",
            "iat": issued,
            "nbf": issued,
            "exp": issued + self.access_ttl_seconds,
            "jti": secrets.token_urlsafe(16),
        }
        return jwt.encode(
            claims, self._signing_key, algorithm=ALGORITHM, headers={"kid": self._key_id}
        )

    def verify_access(self, token: str) -> AccessClaims:
        try:
            header = jwt.get_unverified_header(token)
            key = self._verification_keys.get(str(header.get("kid")))
            if header.get("alg") != ALGORITHM or key is None:
                raise Unauthenticated("token is not valid", code="invalid_token")
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": _REQUIRED_CLAIMS},
                leeway=30,
            )
            if claims.get("typ") != "access":
                raise Unauthenticated("token is not an access token", code="invalid_token")
            return AccessClaims(
                user_id=UUID(claims["sub"]),
                org_id=UUID(claims["org"]),
                token_id=str(claims["jti"]),
                issued_at=int(claims["iat"]),
                expires_at=int(claims["exp"]),
            )
        except jwt.ExpiredSignatureError as exc:
            raise Unauthenticated("token has expired", code="token_expired") from exc
        except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
            raise Unauthenticated("token is not valid", code="invalid_token") from exc

    def jwks(self) -> dict[str, list[dict[str, Any]]]:
        keys = []
        for kid, public in self._verification_keys.items():
            jwk = OKPAlgorithm.to_jwk(public, as_dict=True)
            keys.append({**jwk, "kid": kid, "alg": ALGORITHM, "use": "sig"})
        return {"keys": keys}
