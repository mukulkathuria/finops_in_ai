"""US-01/US-03 token security: EdDSA pinned, kid required, tampering and confusion rejected."""

import base64
import json
import time
from uuid import uuid7

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from gateway.adapters.security.tokens import (
    TokenService,
    b64url_encode,
    parse_public_keys,
    private_key_from_seed,
)
from gateway.domain.errors import Unauthenticated


def _service(signer: Ed25519PrivateKey | None = None, kid: str = "k1", **kw: object) -> TokenService:
    return TokenService(
        signing_key=signer or Ed25519PrivateKey.generate(),
        key_id=kid,
        issuer="finops-gateway",
        audience="finops",
        access_ttl_seconds=900,
        **kw,  # type: ignore[arg-type]
    )


def _b64(obj: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def test_round_trip_and_fifteen_minute_expiry() -> None:
    svc, user, org = _service(), uuid7(), uuid7()
    claims = svc.verify_access(svc.issue_access(user, org))
    assert (claims.user_id, claims.org_id) == (user, org)
    assert claims.expires_at - claims.issued_at == 900


def test_expired_token_is_rejected() -> None:
    svc = _service()
    token = svc.issue_access(uuid7(), uuid7(), now=int(time.time()) - 2000)
    with pytest.raises(Unauthenticated) as err:
        svc.verify_access(token)
    assert err.value.code == "token_expired"


def test_alg_none_is_rejected() -> None:
    svc = _service()
    payload = jwt.decode(svc.issue_access(uuid7(), uuid7()), options={"verify_signature": False})
    forged = f"{_b64({'alg': 'none', 'typ': 'JWT', 'kid': 'k1'})}.{_b64(payload)}."
    with pytest.raises(Unauthenticated):
        svc.verify_access(forged)


def test_hs256_key_confusion_is_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    svc = _service(key)
    public_raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    payload = jwt.decode(svc.issue_access(uuid7(), uuid7()), options={"verify_signature": False})
    forged = jwt.encode(payload, public_raw, algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(Unauthenticated):
        svc.verify_access(forged)


def test_tampered_payload_is_rejected() -> None:
    svc = _service()
    header, payload, sig = svc.issue_access(uuid7(), uuid7()).split(".")
    claims = jwt.decode(f"{header}.{payload}.{sig}", options={"verify_signature": False})
    claims["org"] = str(uuid7())
    with pytest.raises(Unauthenticated):
        svc.verify_access(f"{header}.{_b64(claims)}.{sig}")


def test_token_from_another_key_or_unknown_kid_is_rejected() -> None:
    mine, theirs = _service(), _service(kid="k1")
    with pytest.raises(Unauthenticated):
        mine.verify_access(theirs.issue_access(uuid7(), uuid7()))
    with pytest.raises(Unauthenticated):
        mine.verify_access(_service(kid="unknown").issue_access(uuid7(), uuid7()))


def test_wrong_audience_is_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    other = TokenService(
        signing_key=key,
        key_id="k1",
        issuer="finops-gateway",
        audience="someone-else",
        access_ttl_seconds=900,
    )
    with pytest.raises(Unauthenticated):
        _service(key).verify_access(other.issue_access(uuid7(), uuid7()))


def test_rotation_keeps_previous_key_valid_and_published() -> None:
    old = Ed25519PrivateKey.generate()
    old_token = _service(old, kid="k1").issue_access(uuid7(), uuid7())
    old_pub = b64url_encode(old.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    rotated = _service(kid="k2", previous_keys=parse_public_keys(f"k1:{old_pub}"))
    assert rotated.verify_access(old_token).token_id
    assert {k["kid"] for k in rotated.jwks()["keys"]} == {"k1", "k2"}
    assert all(k["alg"] == "EdDSA" and k["crv"] == "Ed25519" for k in rotated.jwks()["keys"])


def test_seed_and_public_key_parsing_validate_input() -> None:
    with pytest.raises(ValueError, match="32-byte"):
        private_key_from_seed(b64url_encode(b"short"))
    with pytest.raises(ValueError, match="kid:base64url"):
        parse_public_keys("no-colon-here")
    assert parse_public_keys("") == {}
